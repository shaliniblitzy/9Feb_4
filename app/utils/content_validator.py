"""
Five-stage content validation pipeline for the Reverse Document Generator.

Applied to every generated or updated document section to ensure output quality
before persistence to Google Cloud Storage. This module implements layer 2 of
the 5-layer error handling architecture (FR-8: Multi-Layered Error Handling).

Validation Stages:
    1. Non-empty check — ensures content is not None, empty, or whitespace-only
    2. Delimiter pairing — verifies matching pairs of code fences, brackets, etc.
    3. Content cleaning — strips artifacts, duplicate headings, stray fragments
    4. Heading format — validates Markdown heading structure and hierarchy
    5. First-section structure — verifies first section has proper format

Usage::

    from app.utils.content_validator import validate_content, ContentValidationError

    is_valid, cleaned, error = validate_content(section_content)
    if not is_valid:
        raise ContentValidationError(error)

Notes:
    - Stage 3 (cleaning) is a transform, not a gate — it always applies its
      changes and never causes pipeline failure on its own.
    - All other stages are validation gates — failure at any stage halts the
      pipeline and returns a descriptive error message.
    - The pipeline is designed for LLM-generated Markdown tech-spec content
      where structural correctness is critical for downstream consumption.
"""

import re
from typing import Tuple, Optional

from blitzy_utils.logger import BlitzyLogger

# ---------------------------------------------------------------------------
# Module-level logger for structured observability across all 5 stages
# ---------------------------------------------------------------------------
logger = BlitzyLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns — compiled once at module load for performance
# when validating multiple sections in sequence during document generation.
# ---------------------------------------------------------------------------

# Stage 2: Triple-backtick code fence detection (line-start anchored)
_TRIPLE_BACKTICK_PATTERN = re.compile(r"^```", re.MULTILINE)

# Stage 3: Content cleaning patterns
_CONSECUTIVE_BLANK_LINES_PATTERN = re.compile(r"\n{4,}")
_TRAILING_LINE_WHITESPACE_PATTERN = re.compile(r"[ \t]+$", re.MULTILINE)

# Stage 4: Heading detection and validation patterns
_HEADING_LINE_PATTERN = re.compile(r"^(#{1,6})(.*?)$", re.MULTILINE)
_MALFORMED_HEADING_PATTERN = re.compile(r"^#{1,6}[^ \t\n#]", re.MULTILINE)

# Stage 5: First heading extraction
_FIRST_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)$", re.MULTILINE)

# Helper: code fence block removal for delimiter checking outside code
_CODE_FENCE_BLOCK_PATTERN = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


# ---------------------------------------------------------------------------
# ContentValidationError — custom exception for strict-mode validation
# ---------------------------------------------------------------------------


class ContentValidationError(Exception):
    """Custom exception raised when content validation fails in strict mode.

    Carries a descriptive error message from the failing validation stage and
    optionally records which stage (1–5) produced the failure.

    Attributes:
        stage: The validation stage number (1–5) that failed, or ``None`` if
            the failure is not stage-specific.
        message: Human-readable description of the validation failure.
    """

    def __init__(self, message: str, stage: Optional[int] = None) -> None:
        """Initialise with a descriptive message and optional stage number.

        Args:
            message: Description of the validation failure.
            stage: Optional stage number (1–5) where the failure occurred.
        """
        self.stage = stage
        self.message = message
        stage_info = f" at stage {stage}" if stage is not None else ""
        super().__init__(f"Content validation failed{stage_info}: {message}")


# ---------------------------------------------------------------------------
# Stage 1: Non-empty check
# ---------------------------------------------------------------------------


def validate_non_empty(content: Optional[str]) -> Tuple[bool, str]:
    """Stage 1 — verify content is not None, empty, or whitespace-only.

    This is the first gate in the pipeline.  If content is absent or purely
    whitespace no further processing is meaningful.

    Args:
        content: The document section content to validate.  May be ``None``.

    Returns:
        A ``(is_valid, error_message)`` tuple.  ``error_message`` is an empty
        string when validation passes.
    """
    if content is None:
        logger.warning("stage_1_non_empty_check_failed", reason="content_is_none")
        return (False, "Content is None — cannot validate empty content")

    if not isinstance(content, str):
        logger.warning(
            "stage_1_non_empty_check_failed",
            reason="content_not_string",
            content_type=type(content).__name__,
        )
        return (
            False,
            f"Content is not a string — received {type(content).__name__}",
        )

    if len(content) == 0:
        logger.warning("stage_1_non_empty_check_failed", reason="content_is_empty")
        return (False, "Content is an empty string — section must contain text")

    if content.strip() == "":
        logger.warning(
            "stage_1_non_empty_check_failed",
            reason="content_is_whitespace_only",
            length=len(content),
        )
        return (
            False,
            "Content contains only whitespace — section must contain substantive text",
        )

    logger.debug("stage_1_non_empty_check_passed", content_length=len(content))
    return (True, "")


# ---------------------------------------------------------------------------
# Stage 2: Delimiter pairing verification
# ---------------------------------------------------------------------------


def _strip_code_blocks(content: str) -> str:
    """Remove fenced code-block interiors and inline code spans.

    Used by :func:`validate_delimiter_pairing` to prevent false-positive
    bracket / parenthesis / brace mismatches caused by code examples inside
    Markdown fenced blocks or inline code.

    Args:
        content: Raw Markdown content that may contain code blocks.

    Returns:
        Content with fenced code-block bodies and inline code spans removed.
    """
    # Strip fenced code blocks (```…```) and their entire body
    result = _CODE_FENCE_BLOCK_PATTERN.sub("", content)
    # Strip inline code spans (`…`)
    result = _INLINE_CODE_PATTERN.sub("", result)
    return result


def validate_delimiter_pairing(content: str) -> Tuple[bool, str]:
    """Stage 2 — verify matching delimiter pairs in the content.

    Checks the following delimiter categories:

    * **Triple backticks** (````` ``` `````) — must occur an even number of
      times so every code fence has a matching close.  This is the most
      critical check because an unclosed code block corrupts all subsequent
      Markdown formatting.
    * **Parentheses** ``( )`` — checked outside code blocks.
    * **Square brackets** ``[ ]`` — checked outside code blocks.
    * **Curly braces** ``{ }`` — checked outside code blocks.

    Bracket / paren / brace counts are performed *after* stripping code
    block interiors to avoid false positives from code examples.

    Args:
        content: The document section content to validate.

    Returns:
        A ``(is_valid, error_message)`` tuple.  ``error_message`` lists every
        mismatched delimiter category separated by ``"; "``.
    """
    errors: list[str] = []

    # --- Triple backtick pairing (most critical for Markdown) ---
    triple_backtick_matches = _TRIPLE_BACKTICK_PATTERN.findall(content)
    triple_backtick_count = len(triple_backtick_matches)
    if triple_backtick_count % 2 != 0:
        errors.append(
            f"Unmatched triple backticks: found {triple_backtick_count} "
            f"occurrences (expected even count for paired code fence blocks)"
        )
        logger.warning(
            "stage_2_delimiter_mismatch",
            delimiter="triple_backtick",
            count=triple_backtick_count,
        )

    # Strip code blocks before checking brackets / parens / braces
    non_code_content = _strip_code_blocks(content)

    # --- Parenthesis pairing ---
    open_parens = len(re.findall(r"\(", non_code_content))
    close_parens = len(re.findall(r"\)", non_code_content))
    if open_parens != close_parens:
        errors.append(
            f"Unmatched parentheses: {open_parens} opening '(' vs "
            f"{close_parens} closing ')'"
        )
        logger.warning(
            "stage_2_delimiter_mismatch",
            delimiter="parentheses",
            opening=open_parens,
            closing=close_parens,
        )

    # --- Square bracket pairing ---
    open_brackets = len(re.findall(r"\[", non_code_content))
    close_brackets = len(re.findall(r"\]", non_code_content))
    if open_brackets != close_brackets:
        errors.append(
            f"Unmatched square brackets: {open_brackets} opening '[' vs "
            f"{close_brackets} closing ']'"
        )
        logger.warning(
            "stage_2_delimiter_mismatch",
            delimiter="square_brackets",
            opening=open_brackets,
            closing=close_brackets,
        )

    # --- Curly brace pairing ---
    open_braces = len(re.findall(r"\{", non_code_content))
    close_braces = len(re.findall(r"\}", non_code_content))
    if open_braces != close_braces:
        errors.append(
            f"Unmatched curly braces: {open_braces} opening '{{' vs "
            f"{close_braces} closing '}}'"
        )
        logger.warning(
            "stage_2_delimiter_mismatch",
            delimiter="curly_braces",
            opening=open_braces,
            closing=close_braces,
        )

    if errors:
        combined_error = "; ".join(errors)
        logger.warning(
            "stage_2_delimiter_pairing_failed",
            error_count=len(errors),
            errors=combined_error,
        )
        return (False, combined_error)

    logger.debug("stage_2_delimiter_pairing_passed")
    return (True, "")


# ---------------------------------------------------------------------------
# Stage 3: Content cleaning (transform — never fails)
# ---------------------------------------------------------------------------


def clean_content(content: str) -> str:
    """Stage 3 — clean unwanted artifacts from LLM-generated content.

    Unlike other stages this is a *transform*, not a validation gate.  It
    always returns cleaned content and never causes pipeline failure.

    Cleaning operations applied (in order):

    1. Strip leading / trailing whitespace from the entire content block.
    2. Remove consecutive duplicate headings (identical heading text appearing
       on adjacent lines).
    3. Remove a trailing incomplete sentence at the very end of the content
       when the last line is a non-heading, non-list, non-table line that
       does not end with standard terminal punctuation.
    4. Collapse excessive blank lines (4+ consecutive newlines → 2).
    5. Strip trailing whitespace from every line.

    Args:
        content: The document section content to clean.

    Returns:
        The cleaned content string.
    """
    if not content or not content.strip():
        return content

    cleaned = content
    transforms_applied: list[str] = []

    # 1. Strip outer whitespace
    stripped = cleaned.strip()
    if stripped != cleaned:
        transforms_applied.append("stripped_outer_whitespace")
    cleaned = stripped

    # 2. Remove consecutive duplicate headings
    lines = cleaned.split("\n")
    deduped_lines: list[str] = []
    prev_heading_text: Optional[str] = None
    for line in lines:
        stripped_line = line.strip()
        # Detect heading lines
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped_line)
        if heading_match:
            heading_text = stripped_line
            if heading_text == prev_heading_text:
                # Skip this exact duplicate heading
                transforms_applied.append(
                    f"removed_duplicate_heading: {heading_text[:50]}"
                )
                continue
            prev_heading_text = heading_text
        else:
            # Reset heading tracker when a non-heading line is seen
            if stripped_line:
                prev_heading_text = None
        deduped_lines.append(line)
    cleaned = "\n".join(deduped_lines)

    # 3. Remove trailing incomplete sentence at end of content
    final_lines = cleaned.rstrip().split("\n")
    if final_lines:
        last_line = final_lines[-1].strip()
        # Only consider non-empty, non-structural lines (skip headings,
        # lists, tables, blockquotes, code fences, horizontal rules)
        structural_prefixes = (
            "#",    # heading
            "-",    # list or horizontal rule
            "*",    # list or horizontal rule
            "+",    # list
            "|",    # table
            ">",    # blockquote
            "`",    # code fence
            "~",    # code fence (tilde variant)
        )
        terminal_punctuation = (".", "!", "?", ":", ";", ")", "]", "}", '"',
                                "'", "*", "`", "|", "-", "~")
        is_ordered_list = bool(re.match(r"^\d+\.\s", last_line))
        is_structural = (
            not last_line
            or last_line.startswith(structural_prefixes)
            or is_ordered_list
        )
        if (
            not is_structural
            and len(last_line) > 10
            and not last_line.endswith(terminal_punctuation)
        ):
            final_lines.pop()
            transforms_applied.append(
                f"removed_trailing_incomplete_sentence: {last_line[:50]}"
            )
            cleaned = "\n".join(final_lines)

    # 4. Collapse excessive blank lines (4+ newlines → 3, i.e. two blank lines)
    new_cleaned = _CONSECUTIVE_BLANK_LINES_PATTERN.sub("\n\n\n", cleaned)
    if new_cleaned != cleaned:
        transforms_applied.append("collapsed_excessive_blank_lines")
        cleaned = new_cleaned

    # 5. Strip trailing whitespace from each line
    new_cleaned = _TRAILING_LINE_WHITESPACE_PATTERN.sub("", cleaned)
    if new_cleaned != cleaned:
        transforms_applied.append("stripped_trailing_line_whitespace")
        cleaned = new_cleaned

    # Final trim to ensure no leading/trailing whitespace remains
    cleaned = cleaned.strip()

    if transforms_applied:
        logger.info(
            "stage_3_content_cleaning_applied",
            transforms_count=len(transforms_applied),
            transforms=transforms_applied,
        )
    else:
        logger.debug("stage_3_content_cleaning_no_changes")

    return cleaned


# ---------------------------------------------------------------------------
# Stage 4: Heading format validation
# ---------------------------------------------------------------------------


def validate_heading_format(content: str) -> Tuple[bool, str]:
    """Stage 4 — validate Markdown heading format and hierarchy.

    Checks every heading line in the content for:

    * **Proper format** — each heading must have at least one space between
      the ``#`` characters and the heading text (e.g. ``## Title`` not
      ``##Title``).
    * **Non-empty text** — a heading consisting solely of ``#`` characters
      with no text is invalid.
    * **Hierarchy continuity** — heading levels must not skip (e.g. jumping
      from ``##`` directly to ``####`` without an intervening ``###``).
      Going *up* to a shallower level is always permitted.

    Args:
        content: The document section content to validate.

    Returns:
        A ``(is_valid, error_message)`` tuple describing any heading issues.
    """
    errors: list[str] = []

    # Find all lines that look like headings (start with 1–6 '#' characters)
    all_heading_matches = _HEADING_LINE_PATTERN.findall(content)

    if not all_heading_matches:
        # No headings at all — valid for sub-section content fragments
        logger.debug("stage_4_heading_format_no_headings_found")
        return (True, "")

    heading_levels: list[int] = []

    for hashes, rest in all_heading_matches:
        level = len(hashes)
        full_line = hashes + rest

        # Check: space required after hash characters
        if rest and not rest.startswith((" ", "\t")):
            errors.append(
                f"Malformed heading — missing space after '#' characters: "
                f"'{full_line.strip()[:60]}'"
            )
            logger.warning(
                "stage_4_heading_format_violation",
                issue="missing_space_after_hashes",
                heading=full_line.strip()[:60],
            )
            continue

        # Check: heading must have text content
        heading_text = rest.strip() if rest else ""
        if not heading_text:
            errors.append(
                f"Empty heading at level {level} — heading must contain text"
            )
            logger.warning(
                "stage_4_heading_format_violation",
                issue="empty_heading",
                level=level,
            )
            continue

        heading_levels.append(level)

    # Check heading hierarchy — no skipping levels when going deeper
    if len(heading_levels) >= 2:
        for i in range(1, len(heading_levels)):
            current_level = heading_levels[i]
            previous_level = heading_levels[i - 1]
            # Going deeper by more than 1 level indicates a skipped level
            # Going shallower (or same level) is always valid
            if current_level > previous_level + 1:
                errors.append(
                    f"Heading hierarchy skip: jumped from level "
                    f"{previous_level} to level {current_level} "
                    f"(skipped level {previous_level + 1})"
                )
                logger.warning(
                    "stage_4_heading_hierarchy_skip",
                    from_level=previous_level,
                    to_level=current_level,
                )

    if errors:
        combined_error = "; ".join(errors)
        logger.warning(
            "stage_4_heading_format_failed",
            error_count=len(errors),
            errors=combined_error,
        )
        return (False, combined_error)

    logger.debug(
        "stage_4_heading_format_passed",
        heading_count=len(heading_levels),
    )
    return (True, "")


# ---------------------------------------------------------------------------
# Stage 5: First-section structure check
# ---------------------------------------------------------------------------


def validate_first_section_structure(content: str) -> Tuple[bool, str]:
    """Stage 5 — verify the first section follows expected structure.

    A well-formed tech-spec section must:

    * Start with an appropriate heading level (levels 1–6).
    * Contain substantive body content *after* the heading — not just the
      heading itself or immediately followed by a same/higher-level heading.
    * Include at least one line of descriptive text (not exclusively
      sub-headings or horizontal rules).

    If the content does *not* start with a heading the validator checks for
    at least one line of substantive text as a minimal structural requirement.

    Args:
        content: The document section content to validate.

    Returns:
        A ``(is_valid, error_message)`` tuple describing structural issues.
    """
    if not content or not content.strip():
        logger.warning("stage_5_first_section_empty")
        return (False, "Content is empty — cannot validate first section structure")

    trimmed = content.strip()
    lines = trimmed.split("\n")
    errors: list[str] = []

    # Attempt to match the first heading in the content
    first_heading_match = _FIRST_HEADING_PATTERN.match(trimmed)

    if not first_heading_match:
        # ---- Content does not start with a heading ----
        first_line = lines[0].strip()
        if not first_line:
            errors.append(
                "First section does not start with content — leading blank line"
            )
        else:
            # Content starts with non-heading text — acceptable for
            # sub-section fragments.  Verify there is substantive content.
            logger.debug(
                "stage_5_first_section_no_heading",
                first_line=first_line[:60],
            )
            substantive_lines = [
                line
                for line in lines
                if line.strip() and not re.match(r"^#{1,6}\s", line.strip())
            ]
            if len(substantive_lines) < 1:
                errors.append(
                    "First section has no substantive content — "
                    "section must contain descriptive text"
                )
    else:
        # ---- Content starts with a heading ----
        heading_level = len(first_heading_match.group(1))
        heading_text = first_heading_match.group(2).strip()

        # Verify body content exists after the heading
        content_after_heading = "\n".join(lines[1:]).strip()

        if not content_after_heading:
            errors.append(
                f"Section '{heading_text[:50]}' contains only a heading "
                f"with no body content — sections must include descriptive text"
            )
            logger.warning(
                "stage_5_heading_only_section",
                heading=heading_text[:50],
                level=heading_level,
            )
        else:
            # Check whether the next non-empty line is immediately another
            # heading at the same or higher (shallower) level — that would
            # indicate the current section has no body.
            content_lines_after = [
                line for line in lines[1:] if line.strip()
            ]
            if content_lines_after:
                next_line = content_lines_after[0].strip()
                next_heading_match = re.match(r"^(#{1,6})\s", next_line)
                if next_heading_match:
                    next_level = len(next_heading_match.group(1))
                    if next_level <= heading_level:
                        errors.append(
                            f"Section '{heading_text[:50]}' is immediately "
                            f"followed by a same-level or higher-level heading "
                            f"with no body content between them"
                        )

            # Verify substantive body content exists (not just sub-headings
            # or dividers like --- / ===)
            substantive_lines = [
                line
                for line in lines[1:]
                if (
                    line.strip()
                    and not re.match(r"^#{1,6}\s", line.strip())
                    and not line.strip().startswith("---")
                    and not line.strip().startswith("===")
                )
            ]
            if not substantive_lines:
                errors.append(
                    f"Section '{heading_text[:50]}' has no substantive body "
                    f"content — only sub-headings or dividers found"
                )

    if errors:
        combined_error = "; ".join(errors)
        logger.warning(
            "stage_5_first_section_structure_failed",
            error_count=len(errors),
            errors=combined_error,
        )
        return (False, combined_error)

    logger.debug("stage_5_first_section_structure_passed")
    return (True, "")


# ---------------------------------------------------------------------------
# Main orchestrator: validate_content
# ---------------------------------------------------------------------------


def validate_content(content: Optional[str]) -> Tuple[bool, str, str]:
    """Run the complete 5-stage content validation pipeline.

    This is the primary entry point for content validation in the Reverse
    Document Generator workflow.  It is called after each section is
    generated (GENERATE mode) or updated (UPDATE mode) to ensure output
    quality before the section is persisted to Google Cloud Storage.

    **Pipeline execution order:**

    1. **Non-empty check** — reject ``None`` / empty / whitespace content.
    2. **Delimiter pairing** — reject content with unclosed code blocks or
       mismatched bracket/paren/brace counts.
    3. **Content cleaning** — *always* apply cleaning transforms (this stage
       cannot cause failure).
    4. **Heading format** — reject content with malformed Markdown headings.
    5. **First-section structure** — reject structurally invalid sections.

    If Stage 1 fails the original ``content`` (or empty string when ``None``)
    is returned as ``cleaned_content``.  If Stage 2 fails the cleaning
    transform is still applied before returning so the caller can inspect
    the cleaned output alongside the error.

    Args:
        content: The document section content to validate.  May be ``None``.

    Returns:
        A ``(is_valid, cleaned_content, error_message)`` tuple:

        * ``is_valid`` — ``True`` when all validation stages pass.
        * ``cleaned_content`` — content after Stage 3 cleaning transforms
          (or the original / empty string if the pipeline fails before
          Stage 3).
        * ``error_message`` — empty string on success; descriptive error
          on failure.
    """
    logger.info(
        "content_validation_started",
        content_length=len(content) if content else 0,
    )

    # ------------------------------------------------------------------
    # Stage 1: Non-empty check
    # ------------------------------------------------------------------
    is_valid, error = validate_non_empty(content)
    if not is_valid:
        logger.warning("content_validation_failed", stage=1, error=error)
        return (False, content if content is not None else "", error)

    # After Stage 1 we know content is a non-empty, non-whitespace string.
    # The type narrowing below satisfies type-checkers.
    assert isinstance(content, str)

    # ------------------------------------------------------------------
    # Stage 2: Delimiter pairing verification
    # ------------------------------------------------------------------
    is_valid, error = validate_delimiter_pairing(content)
    if not is_valid:
        logger.warning("content_validation_failed", stage=2, error=error)
        # Still apply cleaning so the caller gets the best available output
        cleaned = clean_content(content)
        return (False, cleaned, error)

    # ------------------------------------------------------------------
    # Stage 3: Content cleaning (always applied — transform, not gate)
    # ------------------------------------------------------------------
    cleaned = clean_content(content)

    # ------------------------------------------------------------------
    # Stage 4: Heading format validation (on cleaned content)
    # ------------------------------------------------------------------
    is_valid, error = validate_heading_format(cleaned)
    if not is_valid:
        logger.warning("content_validation_failed", stage=4, error=error)
        return (False, cleaned, error)

    # ------------------------------------------------------------------
    # Stage 5: First-section structure check (on cleaned content)
    # ------------------------------------------------------------------
    is_valid, error = validate_first_section_structure(cleaned)
    if not is_valid:
        logger.warning("content_validation_failed", stage=5, error=error)
        return (False, cleaned, error)

    # ------------------------------------------------------------------
    # All stages passed
    # ------------------------------------------------------------------
    logger.info(
        "content_validation_passed",
        original_length=len(content),
        cleaned_length=len(cleaned),
    )
    return (True, cleaned, "")


# ---------------------------------------------------------------------------
# Public API — explicit export list for star-imports and tooling
# ---------------------------------------------------------------------------
__all__ = [
    "validate_content",
    "ContentValidationError",
    "validate_non_empty",
    "validate_delimiter_pairing",
    "clean_content",
    "validate_heading_format",
    "validate_first_section_structure",
]
