"""
Attachment processing utility for the Reverse Document Generator Flask application.

Downloads binary attachments (Figma screenshots, user-uploaded images, and other
files) from remote URLs via HTTP, converts them to Base64-encoded strings suitable
for embedding in LLM API request payloads, caches results to avoid redundant
downloads, and formats the data for specific LLM provider APIs.

Supported LLM Provider Formats:
    - **Claude (Anthropic)**: Image content blocks with ``cache_control``
      set to ``{"type": "ephemeral"}`` to leverage Anthropic's prompt caching
      for repeated attachment references within a single context window.
    - **OpenAI**: Image URL content blocks using inline ``data:`` URI scheme
      with Base64-encoded binary data.

Caching Strategy:
    Attachment downloads are cached in a per-job ``attachment_base64_cache`` dict
    (keyed by attachment URL) to prevent redundant HTTP downloads when the same
    attachment is referenced multiple times during a single document generation
    or update workflow execution. The cache dict is scoped per-job within
    ``ReverseDocumentState`` and is not shared across concurrent jobs.

Error Handling:
    Individual attachment download failures are logged and gracefully skipped
    rather than raising exceptions that would abort the entire document
    generation workflow. This ensures partial availability — if 4 out of 5
    attachments download successfully, the workflow continues with those 4.

This module supports Feature F-006 (Attachment Processing and Multi-Modal Input)
and is used by the LangGraph workflow nodes in ``app/services/workflow/nodes.py``
during context gathering and document authoring phases.
"""

import base64
from typing import Any, Dict, List, Optional

import httpx

from blitzy_utils.logger import BlitzyLogger

# ---------------------------------------------------------------------------
# Module-level structured logger for attachment processing operations.
# Logs include: successful downloads, cache hits, download failures with
# error context, and graceful skip messages for individual failures.
# ---------------------------------------------------------------------------
logger = BlitzyLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP client configuration for attachment downloads.
# Timeout is set generously to accommodate large image files served from
# external services (Admin Service attachment URLs, Figma CDN, etc.).
# ---------------------------------------------------------------------------
_DEFAULT_DOWNLOAD_TIMEOUT: float = 30.0


def download_attachment_as_base64(
    url: str,
    timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT,
) -> str:
    """Download a binary attachment from a URL and return its Base64-encoded content.

    Performs an HTTP GET request to the specified URL, reads the full response
    body as raw bytes, and encodes it using Base64. The resulting bytes are
    decoded to a UTF-8 string suitable for direct embedding in JSON payloads
    for both Claude and OpenAI API calls.

    Args:
        url: The fully-qualified HTTP(S) URL of the attachment to download.
            Typically an Admin Service attachment URL or Figma CDN image URL.
        timeout: Maximum time in seconds to wait for the HTTP response.
            Defaults to 30 seconds to accommodate large image files.

    Returns:
        A Base64-encoded string representation of the downloaded binary content.

    Raises:
        httpx.HTTPStatusError: If the HTTP response status code indicates an
            error (4xx or 5xx). Callers should handle this for graceful degradation.
        httpx.RequestError: If the HTTP request fails due to network issues
            (DNS resolution failure, connection timeout, etc.).
        ValueError: If the provided URL is empty or None.
    """
    if not url:
        raise ValueError("Attachment URL must not be empty or None")

    logger.debug(
        "downloading_attachment",
        url=url,
        timeout=timeout,
    )

    # Perform synchronous HTTP GET with follow_redirects enabled to handle
    # CDN redirects commonly used by attachment storage services.
    response: httpx.Response = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
    )

    # Raise an HTTPStatusError for 4xx/5xx responses, providing detailed
    # error information including the status code and response body excerpt.
    response.raise_for_status()

    # Encode the raw binary content (bytes) to Base64 and decode to a
    # UTF-8 string for JSON-serializable embedding in LLM API payloads.
    raw_content: bytes = response.content
    base64_encoded: str = base64.b64encode(raw_content).decode("utf-8")

    logger.info(
        "attachment_downloaded_successfully",
        url=url,
        content_size_bytes=len(raw_content),
        base64_size_chars=len(base64_encoded),
    )

    return base64_encoded


def process_attachments(
    attachments: List[Dict[str, Any]],
    cache: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Process a list of attachments, downloading and caching their Base64 content.

    Iterates over the provided attachment list, checking the
    ``attachment_base64_cache`` dict for previously downloaded content to avoid
    redundant HTTP downloads. For cache misses, calls
    ``download_attachment_as_base64()`` to fetch and encode the content, then
    stores the result in the cache dict for future lookups.

    Each attachment dict is expected to have at minimum a ``url`` key containing
    the download URL. The function enriches each attachment dict with a
    ``base64_data`` key containing the cached or freshly downloaded content.

    Attachments that fail to download are logged and gracefully skipped —
    they are excluded from the returned list rather than raising an exception
    that would abort the entire workflow.

    Args:
        attachments: A list of attachment dictionaries, each containing at
            minimum a ``url`` key with the download URL. Additional keys
            (e.g., ``media_type``, ``filename``, ``attachment_id``) are
            preserved in the output.
        cache: A mutable dict serving as the ``attachment_base64_cache``,
            keyed by attachment URL with Base64-encoded content as values.
            This dict is typically scoped per-job within the
            ``ReverseDocumentState`` to prevent cross-job contamination.

    Returns:
        A list of enriched attachment dictionaries, each containing the
        original keys plus a ``base64_data`` key with the Base64-encoded
        content. Attachments that failed to download are excluded from
        this list.

    Example::

        cache = {}
        attachments = [
            {"url": "https://example.com/image.png", "media_type": "image/png"},
            {"url": "https://example.com/screenshot.jpg", "media_type": "image/jpeg"},
        ]
        processed = process_attachments(attachments, cache)
        # processed contains enriched dicts with 'base64_data' key
        # cache now contains {"https://example.com/image.png": "iVBOR...", ...}
    """
    if not attachments:
        logger.debug("no_attachments_to_process", count=0)
        return []

    logger.info(
        "processing_attachments",
        total_attachments=len(attachments),
        cache_size_before=len(cache),
    )

    processed: List[Dict[str, Any]] = []

    for index, attachment in enumerate(attachments):
        attachment_url: Optional[str] = attachment.get("url")

        if not attachment_url:
            logger.warning(
                "attachment_missing_url",
                index=index,
                attachment_keys=list(attachment.keys()),
            )
            continue

        try:
            # Check the cache for previously downloaded content to avoid
            # redundant HTTP requests for the same attachment URL.
            if attachment_url in cache:
                logger.debug(
                    "attachment_cache_hit",
                    url=attachment_url,
                    index=index,
                )
                base64_data = cache[attachment_url]
            else:
                # Cache miss — download the attachment and store in cache
                logger.debug(
                    "attachment_cache_miss",
                    url=attachment_url,
                    index=index,
                )
                base64_data = download_attachment_as_base64(attachment_url)
                cache[attachment_url] = base64_data
                logger.debug(
                    "attachment_cached",
                    url=attachment_url,
                    cache_size_after=len(cache),
                )

            # Enrich the attachment dict with the Base64-encoded content.
            # Create a shallow copy to avoid mutating the original input list.
            enriched_attachment: Dict[str, Any] = {**attachment}
            enriched_attachment["base64_data"] = base64_data
            processed.append(enriched_attachment)

        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
            # Gracefully skip failed attachments rather than crashing the
            # entire document generation workflow. Log the failure with
            # sufficient context for debugging.
            logger.error(
                "attachment_download_failed",
                url=attachment_url,
                index=index,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            continue

        except Exception as exc:
            # Catch any unexpected errors to ensure workflow resilience.
            # This includes edge cases like malformed URLs, encoding errors,
            # or unexpected response formats.
            logger.error(
                "attachment_processing_unexpected_error",
                url=attachment_url,
                index=index,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            continue

    logger.info(
        "attachments_processed",
        total_input=len(attachments),
        total_processed=len(processed),
        total_skipped=len(attachments) - len(processed),
        cache_size_after=len(cache),
    )

    return processed


def format_attachment_for_claude(
    base64_data: str,
    media_type: str,
) -> Dict[str, Any]:
    """Format attachment data as a Claude-compatible image content block.

    Creates a message content dictionary conforming to Anthropic's Claude API
    specification for inline Base64 image attachments. Includes the
    ``cache_control`` field set to ``{"type": "ephemeral"}`` to leverage
    Anthropic's prompt caching mechanism, reducing token costs when the same
    attachment is referenced multiple times within a conversation context.

    The ephemeral cache control setting (AAP §0.4.1) ensures that the
    attachment data is cached by the Anthropic API for the duration of the
    request context, avoiding redundant re-processing of large binary payloads
    in multi-turn agent tool-calling loops.

    Args:
        base64_data: The Base64-encoded string of the attachment binary content.
        media_type: The MIME type of the attachment (e.g., ``"image/png"``,
            ``"image/jpeg"``, ``"image/webp"``, ``"image/gif"``).

    Returns:
        A dictionary conforming to Claude's image content block schema::

            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "<base64_encoded_string>"
                },
                "cache_control": {
                    "type": "ephemeral"
                }
            }
    """
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64_data,
        },
        "cache_control": {
            "type": "ephemeral",
        },
    }


def format_attachment_for_openai(
    base64_data: str,
    media_type: str,
) -> Dict[str, Any]:
    """Format attachment data as an OpenAI-compatible image URL content block.

    Creates a message content dictionary conforming to OpenAI's GPT API
    specification for inline Base64 image attachments using the ``data:`` URI
    scheme. This format embeds the Base64-encoded binary data directly in the
    URL field, allowing GPT-5 Mini (used for structured classification in the
    UPDATE workflow) to process image attachments without requiring a separate
    hosted image URL.

    Args:
        base64_data: The Base64-encoded string of the attachment binary content.
        media_type: The MIME type of the attachment (e.g., ``"image/png"``,
            ``"image/jpeg"``, ``"image/webp"``, ``"image/gif"``).

    Returns:
        A dictionary conforming to OpenAI's image URL content block schema::

            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,<base64_encoded_string>"
                }
            }
    """
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media_type};base64,{base64_data}",
        },
    }
