"""
Exponential retry decorator for the Reverse Document Generator Flask application.

Reimplements the @archie_exponential_retry() decorator that wraps critical LangGraph
workflow nodes (setup, gather_context, document_section, update_section,
copy_old_tech_spec_section) with tenacity-based exponential backoff.

Exception Classification:
    - RETRYABLE_EXCEPTIONS: Platform-defined retryable exceptions from
      blitzy_platform_shared.common.consts (API rate limits, transient network
      errors from Claude, OpenAI, GCS, Neo4j).
    - SUPPLEMENTARY_RETRYABLE_EXCEPTIONS: Additional Flask-context retryable
      exceptions (ConnectionError, TimeoutError, etc.) covering transient
      failures in a persistent web server environment.
    - Non-retryable: All other exceptions are logged with full traceback and
      re-raised immediately without retry.

Retry Behavior:
    - Exponential backoff: wait = min(max_wait, min_wait * 2^attempt)
    - Default configuration: 5 attempts, 1s min wait, 60s max wait
    - Tracks retry_count in LangGraph workflow state for observability

This module is layer 1 of the 5-layer error handling architecture:
    1. Application retry (@archie_exponential_retry) <-- THIS MODULE
    2. Content validation (5-stage pipeline)
    3. State restoration (UPDATE mode)
    4. Infrastructure retry (Cloud Run)
    5. Resource cleanup (Neo4j finally block)
"""

from functools import wraps
import asyncio
import logging
import traceback
from typing import Any, Callable, Dict, Optional, Tuple, Type

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

from blitzy_platform_shared.common.consts import RETRYABLE_EXCEPTIONS
from blitzy_utils.logger import BlitzyLogger

# Structured logger for retry module operations including retry tracking,
# exception classification, and state update logging
logger = BlitzyLogger(__name__)

# ---------------------------------------------------------------------------
# Supplementary Retryable Exceptions
# ---------------------------------------------------------------------------
# These extend the platform-defined RETRYABLE_EXCEPTIONS to cover additional
# transient failure modes encountered in a persistent Flask web server
# environment, as opposed to the original single-shot Cloud Run Job model.
#
# The platform RETRYABLE_EXCEPTIONS already covers service-specific transient
# errors (Anthropic rate limits, OpenAI API errors, Neo4j transient failures,
# GCS temporary unavailability). These supplementary exceptions cover lower-level
# network and OS-layer transient failures that may occur between the Flask
# server and any of the 12+ external service integrations.
# ---------------------------------------------------------------------------
SUPPLEMENTARY_RETRYABLE_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    ConnectionError,        # Network connection failures (includes ConnectionResetError,
                            # ConnectionAbortedError, BrokenPipeError as subclasses)
    TimeoutError,           # Operation timeouts (socket, HTTP, database connections)
    ConnectionResetError,   # Remote end forcibly closed the connection
    ConnectionAbortedError,  # Connection attempt aborted by the local host
    BrokenPipeError,        # Write to a closed pipe or socket
    OSError,                # Low-level OS/socket errors (DNS resolution failures,
                            # socket bind errors, file descriptor exhaustion)
)


def _extract_state_from_args(
    args: tuple,
    kwargs: dict,
) -> Optional[Dict[str, Any]]:
    """Extract the LangGraph workflow state dict from function arguments.

    LangGraph node functions receive the workflow state (ReverseDocumentState,
    a TypedDict with 30+ fields) as their first positional argument for
    standalone functions, or as the second argument for bound class methods.
    At runtime, TypedDicts are regular Python dicts.

    This function inspects both keyword arguments (for an explicit 'state'
    parameter) and positional arguments (for dict instances) to locate the
    workflow state, enabling retry_count tracking within the state.

    Args:
        args: Positional arguments passed to the decorated function.
        kwargs: Keyword arguments passed to the decorated function.

    Returns:
        The workflow state dict if found among the arguments, None otherwise.
        Returns None if no dict argument is present, which means retry_count
        tracking will be silently skipped for that invocation.
    """
    # Check keyword arguments first for an explicit 'state' parameter
    if 'state' in kwargs:
        candidate = kwargs['state']
        if isinstance(candidate, dict):
            return candidate

    # Check positional arguments for dict instances.
    # LangGraph nodes typically receive state as the first positional arg
    # (standalone function) or second positional arg (bound method where
    # the first arg is 'self').
    for arg in args:
        if isinstance(arg, dict):
            return arg

    return None


def archie_exponential_retry(
    max_attempts: int = 5,
    min_wait: float = 1.0,
    max_wait: float = 60.0,
) -> Callable:
    """Decorator factory for exponential backoff retry on critical workflow nodes.

    Creates a decorator that wraps both synchronous and asynchronous functions
    with tenacity-based retry logic. Retries are triggered only for exceptions
    classified as retryable (RETRYABLE_EXCEPTIONS from the Blitzy platform
    library combined with SUPPLEMENTARY_RETRYABLE_EXCEPTIONS defined in this
    module).

    The decorator automatically detects whether the target function is a
    coroutine (via asyncio.iscoroutinefunction) and applies the appropriate
    sync or async wrapper.

    State Tracking:
        If the decorated function receives a LangGraph workflow state dict
        (either as a positional or keyword argument), the decorator increments
        ``state['retry_count']`` on each retry attempt, enabling production
        observability of retry frequency per workflow execution.

    Non-Retryable Exception Handling:
        Exceptions not in the retryable categories are logged with full
        traceback via the structured logger and re-raised immediately without
        any retry attempt.

    Args:
        max_attempts: Maximum number of execution attempts including the
            initial attempt. Default is 5 (1 initial + up to 4 retries).
        min_wait: Minimum wait time in seconds between retries. The actual
            wait starts at this value and grows exponentially. Default is 1.0.
        max_wait: Maximum wait time in seconds between retries. Caps the
            exponential growth to prevent excessively long waits. Default
            is 60.0.

    Returns:
        A decorator function that wraps the target function with retry logic.

    Example:
        @archie_exponential_retry(max_attempts=5, min_wait=1.0, max_wait=60.0)
        def setup(state: ReverseDocumentState) -> dict:
            # Retried up to 5 times on transient API/network errors
            ...

        @archie_exponential_retry()
        async def gather_context(state: ReverseDocumentState) -> dict:
            # Async functions are fully supported with the same retry behavior
            ...
    """
    # Combine platform-defined and supplementary retryable exception tuples
    # into a single tuple for tenacity's retry_if_exception_type filter
    all_retryable_exceptions: Tuple[Type[BaseException], ...] = (
        RETRYABLE_EXCEPTIONS + SUPPLEMENTARY_RETRYABLE_EXCEPTIONS
    )

    # Standard Python logger for tenacity's before_sleep_log callback.
    # Tenacity's built-in logging requires a standard logging.Logger instance
    # (not a structlog-based logger), so we obtain one via logging.getLogger().
    _tenacity_logger: logging.Logger = logging.getLogger(__name__)

    # Pre-create the tenacity before_sleep logging callback at WARNING level.
    # This emits a log message before each retry sleep with attempt number,
    # exception details, and computed wait time.
    _tenacity_sleep_log_callback = before_sleep_log(
        _tenacity_logger, logging.WARNING
    )

    def decorator(func: Callable) -> Callable:
        """Inner decorator that applies retry logic to the target function."""

        if asyncio.iscoroutinefunction(func):
            # ----------------------------------------------------------
            # Async function wrapper
            # ----------------------------------------------------------
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Async wrapper providing retry logic and state tracking.

                Creates a per-invocation tenacity retry decorator to bind the
                before_sleep callback to the current call's state dict for
                retry_count tracking.
                """
                # Extract workflow state for retry_count tracking
                state = _extract_state_from_args(args, kwargs)

                logger.debug(
                    "retry_decorator_invoked",
                    function=func.__name__,
                    max_attempts=max_attempts,
                    min_wait=min_wait,
                    max_wait=max_wait,
                    is_async=True,
                    has_state=state is not None,
                )

                def _before_sleep_with_tracking(retry_state: Any) -> None:
                    """Combined callback for tenacity logging and state tracking.

                    Delegates to tenacity's standard before_sleep_log for retry
                    attempt logging, then increments retry_count in the workflow
                    state dict if present.
                    """
                    # Emit tenacity's standard retry sleep log message
                    _tenacity_sleep_log_callback(retry_state)

                    # Track retry_count in workflow state for observability
                    if state is not None:
                        current_count = state.get('retry_count', 0)
                        state['retry_count'] = current_count + 1
                        logger.info(
                            "retry_count_updated",
                            function=func.__name__,
                            retry_count=state['retry_count'],
                            attempt_number=retry_state.attempt_number,
                            max_attempts=max_attempts,
                        )

                # Build a per-invocation tenacity retry decorator.
                # This is created per-call (not per-decoration) so that the
                # before_sleep callback can capture the current invocation's
                # state dict via closure for retry_count tracking.
                retrying_decorator = retry(
                    stop=stop_after_attempt(max_attempts),
                    wait=wait_exponential(
                        multiplier=1,
                        min=min_wait,
                        max=max_wait,
                    ),
                    retry=retry_if_exception_type(all_retryable_exceptions),
                    before_sleep=_before_sleep_with_tracking,
                    reraise=True,
                )

                # Apply the retry decorator to the original async function
                retried_func = retrying_decorator(func)

                try:
                    return await retried_func(*args, **kwargs)
                except RetryError as retry_err:
                    # Safety fallback: with reraise=True this branch should
                    # not normally execute, but provides defense-in-depth if
                    # tenacity behavior changes in future versions.
                    last_exception = retry_err.last_attempt.exception()
                    logger.error(
                        "retry_error_caught",
                        function=func.__name__,
                        max_attempts=max_attempts,
                        exception_type=(
                            type(last_exception).__name__
                            if last_exception else "Unknown"
                        ),
                        exception_message=(
                            str(last_exception) if last_exception else ""
                        ),
                        retry_count=(
                            state.get('retry_count', 0) if state else 0
                        ),
                    )
                    if last_exception is not None:
                        raise last_exception
                    raise
                except Exception as exc:
                    # Classify the exception for appropriate logging.
                    # With reraise=True, tenacity re-raises the last exception
                    # after all retry attempts are exhausted for retryable
                    # exceptions, or immediately for non-retryable exceptions.
                    if isinstance(exc, all_retryable_exceptions):
                        logger.error(
                            "retry_attempts_exhausted",
                            function=func.__name__,
                            max_attempts=max_attempts,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc),
                            retry_count=(
                                state.get('retry_count', 0) if state else 0
                            ),
                        )
                    else:
                        logger.error(
                            "non_retryable_exception",
                            function=func.__name__,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc),
                            traceback_detail=traceback.format_exc(),
                        )
                    raise

            return async_wrapper

        else:
            # ----------------------------------------------------------
            # Synchronous function wrapper
            # ----------------------------------------------------------
            @wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Sync wrapper providing retry logic and state tracking.

                Creates a per-invocation tenacity retry decorator to bind the
                before_sleep callback to the current call's state dict for
                retry_count tracking.
                """
                # Extract workflow state for retry_count tracking
                state = _extract_state_from_args(args, kwargs)

                logger.debug(
                    "retry_decorator_invoked",
                    function=func.__name__,
                    max_attempts=max_attempts,
                    min_wait=min_wait,
                    max_wait=max_wait,
                    is_async=False,
                    has_state=state is not None,
                )

                def _before_sleep_with_tracking(retry_state: Any) -> None:
                    """Combined callback for tenacity logging and state tracking.

                    Delegates to tenacity's standard before_sleep_log for retry
                    attempt logging, then increments retry_count in the workflow
                    state dict if present.
                    """
                    # Emit tenacity's standard retry sleep log message
                    _tenacity_sleep_log_callback(retry_state)

                    # Track retry_count in workflow state for observability
                    if state is not None:
                        current_count = state.get('retry_count', 0)
                        state['retry_count'] = current_count + 1
                        logger.info(
                            "retry_count_updated",
                            function=func.__name__,
                            retry_count=state['retry_count'],
                            attempt_number=retry_state.attempt_number,
                            max_attempts=max_attempts,
                        )

                # Build a per-invocation tenacity retry decorator.
                # Created per-call to bind the before_sleep callback to the
                # current invocation's state dict via closure.
                retrying_decorator = retry(
                    stop=stop_after_attempt(max_attempts),
                    wait=wait_exponential(
                        multiplier=1,
                        min=min_wait,
                        max=max_wait,
                    ),
                    retry=retry_if_exception_type(all_retryable_exceptions),
                    before_sleep=_before_sleep_with_tracking,
                    reraise=True,
                )

                # Apply the retry decorator to the original sync function
                retried_func = retrying_decorator(func)

                try:
                    return retried_func(*args, **kwargs)
                except RetryError as retry_err:
                    # Safety fallback: with reraise=True this branch should
                    # not normally execute, but provides defense-in-depth if
                    # tenacity behavior changes in future versions.
                    last_exception = retry_err.last_attempt.exception()
                    logger.error(
                        "retry_error_caught",
                        function=func.__name__,
                        max_attempts=max_attempts,
                        exception_type=(
                            type(last_exception).__name__
                            if last_exception else "Unknown"
                        ),
                        exception_message=(
                            str(last_exception) if last_exception else ""
                        ),
                        retry_count=(
                            state.get('retry_count', 0) if state else 0
                        ),
                    )
                    if last_exception is not None:
                        raise last_exception
                    raise
                except Exception as exc:
                    # Classify the exception for appropriate logging.
                    if isinstance(exc, all_retryable_exceptions):
                        logger.error(
                            "retry_attempts_exhausted",
                            function=func.__name__,
                            max_attempts=max_attempts,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc),
                            retry_count=(
                                state.get('retry_count', 0) if state else 0
                            ),
                        )
                    else:
                        logger.error(
                            "non_retryable_exception",
                            function=func.__name__,
                            exception_type=type(exc).__name__,
                            exception_message=str(exc),
                            traceback_detail=traceback.format_exc(),
                        )
                    raise

            return sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
# archie_exponential_retry: Decorator factory for workflow node retry logic
# RETRYABLE_EXCEPTIONS: Re-exported platform-defined retryable exception tuple
# SUPPLEMENTARY_RETRYABLE_EXCEPTIONS: Flask-context retryable exception tuple
# ---------------------------------------------------------------------------
__all__ = [
    "archie_exponential_retry",
    "RETRYABLE_EXCEPTIONS",
    "SUPPLEMENTARY_RETRYABLE_EXCEPTIONS",
]
