"""
Background job execution manager for the Reverse Document Generator Flask application.

Provides the JobManager class that implements a thread-safe job registry for dispatching
long-running LangGraph workflow executions as background tasks. This module is the critical
bridge between Flask's synchronous HTTP request/response model (returning 202 Accepted with
a job_id) and the asynchronous LangGraph StateGraph execution that may run for extended
periods during document generation.

Key architectural decisions:
- Uses concurrent.futures.ThreadPoolExecutor for managed background thread execution
- All dictionary operations on the job registry are protected by threading.Lock()
- Each background thread creates its own asyncio event loop to bridge Flask's sync WSGI
  model with LangGraph's async internals (astream/ainvoke)
- Progress callback pattern allows workflow nodes to report per-section progress in real-time
- Automatic cleanup of stale job records prevents unbounded memory growth

Exports:
    JobStatus: Enum with values PENDING, IN_PROGRESS, COMPLETE, FAILED
    JobRecord: Dataclass storing per-job mutable state (status, progress, errors, results)
    JobManager: Thread-safe job registry with submit, query, and cleanup capabilities
"""

import threading
import asyncio
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from blitzy_utils.logger import BlitzyLogger

# Module-level structured logger for job management operations
logger = BlitzyLogger(__name__)


class JobStatus(str, Enum):
    """
    Enumeration of job lifecycle states.

    The str mixin enables direct JSON serialization of status values in API responses
    and structured logging output without requiring explicit .value access.

    State transitions:
        PENDING -> IN_PROGRESS -> COMPLETE
        PENDING -> IN_PROGRESS -> FAILED
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass
class JobRecord:
    """
    Per-job state container tracking the full lifecycle of a background workflow execution.

    Stores immutable identity (job_id, request_payload), mutable status and progress
    counters, timestamps for lifecycle tracking, and result/error data populated upon
    completion or failure.

    All fields are mutable to support thread-safe updates via the JobManager's lock-protected
    methods. The dataclass decorator provides automatic __init__, __repr__, and __eq__.

    Attributes:
        job_id: Unique identifier (UUID4 string) serving as the primary key in the job registry.
        status: Current lifecycle state (PENDING, IN_PROGRESS, COMPLETE, or FAILED).
        created_at: UTC timestamp when the job was submitted to the registry.
        updated_at: UTC timestamp of the most recent status or progress update.
        current_index: Zero-based index of the section currently being processed.
        total_steps: Total number of sections to process in this job.
        section_headings: Ordered list of section headings completed or in progress.
        error: Human-readable error message populated when status is FAILED.
        error_type: Exception class name for programmatic error classification.
        result: Dictionary containing workflow output populated when status is COMPLETE.
        request_payload: Original request data preserved for audit and debugging purposes.
    """

    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    current_index: int = 0
    total_steps: int = 0
    section_headings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    error_type: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    request_payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize the JobRecord to a JSON-compatible dictionary.

        Converts datetime objects to ISO 8601 strings and JobStatus enum to its
        string value for clean API response formatting.

        Returns:
            Dictionary representation of the job record suitable for JSON serialization.
        """
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "current_index": self.current_index,
            "total_steps": self.total_steps,
            "section_headings": list(self.section_headings),
            "error": self.error,
            "error_type": self.error_type,
            "result": self.result,
            "request_payload": self.request_payload,
        }


class JobManager:
    """
    Thread-safe background job execution manager for long-running LangGraph workflows.

    Bridges the gap between Flask's synchronous HTTP request/response model and the
    potentially long-running LangGraph StateGraph execution by:
    1. Accepting workflow functions and dispatching them to a ThreadPoolExecutor
    2. Returning a job_id immediately for client-side status polling
    3. Tracking per-job status, progress, and results in a thread-safe registry
    4. Providing query methods consumed by REST API endpoints

    The executor manages a configurable pool of worker threads (default: 4) that each
    create their own asyncio event loop to run async LangGraph workflows, ensuring
    compatibility with Flask's synchronous WSGI model.

    Thread Safety:
        All operations on the internal _jobs dictionary are protected by a threading.Lock()
        to ensure safe concurrent access from multiple background execution threads and
        Flask request handler threads simultaneously.

    Usage:
        manager = JobManager(max_workers=4)
        job_id = manager.submit_job(workflow_func, request_payload)
        status = manager.get_job_status(job_id)
        progress = manager.get_job_progress(job_id)
    """

    def __init__(self, max_workers: int = 4) -> None:
        """
        Initialize the JobManager with a thread pool and empty job registry.

        Args:
            max_workers: Maximum number of concurrent background workflow threads.
                         Defaults to 4, which balances concurrent document generation
                         requests against system resource constraints (each workflow
                         involves heavy LLM API calls and Neo4j graph operations).
        """
        self._jobs: Dict[str, JobRecord] = {}
        self._lock: threading.Lock = threading.Lock()
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="job-worker",
        )
        self._max_workers = max_workers
        logger.info(
            "JobManager initialized",
            max_workers=max_workers,
        )

    def submit_job(
        self,
        workflow_func: Callable[..., Any],
        request_payload: Dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """
        Submit a workflow function for background execution and return a job ID immediately.

        Creates a new JobRecord in PENDING state, registers it in the thread-safe job
        registry, and dispatches the workflow execution to the ThreadPoolExecutor. The
        caller receives a UUID job_id immediately, enabling the Flask endpoint to return
        HTTP 202 Accepted without blocking.

        Args:
            workflow_func: An async callable that accepts (request_payload, progress_callback, **kwargs)
                          and executes the LangGraph StateGraph workflow. This is typically
                          ReverseDocumentHelper.run() or a wrapper around it.
            request_payload: Dictionary containing the original request data (user_id, company_id,
                           project_id, repo_name, tech_spec_id, document_mode, etc.). Stored
                           in the JobRecord for audit/debugging and passed to workflow_func.
            **kwargs: Additional keyword arguments forwarded to the workflow function (e.g.,
                     configuration overrides, feature flags).

        Returns:
            job_id: UUID4 string uniquely identifying this job for subsequent status polling.

        Raises:
            RuntimeError: If the ThreadPoolExecutor has been shut down.
        """
        job_id = str(uuid.uuid4())
        now = datetime.utcnow()

        job_record = JobRecord(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
            request_payload=dict(request_payload),
        )

        with self._lock:
            self._jobs[job_id] = job_record

        logger.info(
            "Job submitted",
            job_id=job_id,
            document_mode=request_payload.get("document_mode", "unknown"),
            project_id=request_payload.get("project_id", "unknown"),
        )

        # Dispatch background execution to the thread pool.
        # The future is not stored because job tracking is done via the registry,
        # not via Future objects. This avoids complexity with future lifecycle management.
        self._executor.submit(
            self._execute_job,
            job_id,
            workflow_func,
            request_payload,
            **kwargs,
        )

        return job_id

    def _execute_job(
        self,
        job_id: str,
        workflow_func: Callable[..., Any],
        request_payload: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """
        Execute a workflow function in a background thread with full lifecycle management.

        This method runs within a ThreadPoolExecutor worker thread. It:
        1. Transitions the job status from PENDING to IN_PROGRESS
        2. Creates a fresh asyncio event loop for this thread (required because Flask's
           WSGI model doesn't provide one, and LangGraph uses async internally)
        3. Runs the async workflow function to completion
        4. On success: stores the result and transitions to COMPLETE
        5. On failure: captures the full traceback and transitions to FAILED
        6. Always cleans up the asyncio event loop

        The progress_callback is injected into the workflow function, enabling real-time
        per-section progress reporting back to the job registry.

        Args:
            job_id: Unique identifier for this job in the registry.
            workflow_func: Async callable to execute the LangGraph workflow.
            request_payload: Original request data forwarded to the workflow.
            **kwargs: Additional keyword arguments for the workflow function.
        """
        # Transition to IN_PROGRESS
        with self._lock:
            job_record = self._jobs.get(job_id)
            if job_record is None:
                logger.error("Job record not found during execution start", job_id=job_id)
                return
            job_record.status = JobStatus.IN_PROGRESS
            job_record.updated_at = datetime.utcnow()

        logger.info("Job execution started", job_id=job_id)

        # Create a dedicated asyncio event loop for this background thread.
        # Flask's synchronous WSGI model does not provide an event loop, but
        # LangGraph's StateGraph uses async internals (astream, ainvoke).
        # Each thread gets its own loop to avoid conflicts between concurrent jobs.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Build the progress callback bound to this job_id
            progress_callback = self._make_progress_callback(job_id)

            # Execute the async workflow function within the thread's event loop.
            # The workflow function is expected to be an async callable that accepts
            # the request payload, a progress callback, and optional kwargs.
            result = loop.run_until_complete(
                workflow_func(
                    request_payload,
                    progress_callback=progress_callback,
                    **kwargs,
                )
            )

            # Transition to COMPLETE with result data
            with self._lock:
                job_record = self._jobs.get(job_id)
                if job_record is not None:
                    job_record.status = JobStatus.COMPLETE
                    job_record.updated_at = datetime.utcnow()
                    job_record.result = result if isinstance(result, dict) else {"output": result}

            logger.info(
                "Job completed successfully",
                job_id=job_id,
                total_sections=job_record.total_steps if job_record else 0,
            )

        except Exception as exc:
            # Capture full traceback for debugging — never let thread exceptions
            # propagate silently as they would be swallowed by the executor
            error_traceback = traceback.format_exc()
            error_message = str(exc)
            error_type_name = type(exc).__name__

            with self._lock:
                job_record = self._jobs.get(job_id)
                if job_record is not None:
                    job_record.status = JobStatus.FAILED
                    job_record.updated_at = datetime.utcnow()
                    job_record.error = error_message
                    job_record.error_type = error_type_name

            logger.error(
                "Job execution failed",
                job_id=job_id,
                error_type=error_type_name,
                error_message=error_message,
                traceback=error_traceback,
            )

        finally:
            # Clean up the event loop created for this thread to prevent resource leaks.
            # This is essential because each background thread creates its own loop.
            try:
                # Cancel any remaining tasks on the loop before closing
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                # Allow cancelled tasks to finalize
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass  # Best-effort cleanup; loop closure below handles remaining resources
            finally:
                loop.close()

    def _make_progress_callback(self, job_id: str) -> Callable[..., None]:
        """
        Create a progress callback function bound to a specific job ID.

        Returns a callable that workflow nodes (particularly document_section and
        update_section) can invoke to report per-section completion progress. The
        callback updates the job record in the thread-safe registry, enabling the
        GET /api/v1/jobs/{job_id}/progress endpoint to return real-time progress data.

        Args:
            job_id: The job ID this callback is bound to.

        Returns:
            A callable accepting (current_index: int, total_steps: int, section_heading: str)
            that updates the job's progress fields in the registry.
        """

        def progress_callback(
            current_index: int,
            total_steps: int,
            section_heading: Optional[str] = None,
        ) -> None:
            """
            Update job progress in the registry with current section completion data.

            Args:
                current_index: Zero-based index of the section just completed or in progress.
                total_steps: Total number of sections in this document generation job.
                section_heading: Optional heading of the section being processed. Appended
                                to section_headings list if provided and not already present.
            """
            with self._lock:
                job_record = self._jobs.get(job_id)
                if job_record is None:
                    logger.warning(
                        "Progress update for unknown job",
                        job_id=job_id,
                        current_index=current_index,
                    )
                    return

                job_record.current_index = current_index
                job_record.total_steps = total_steps
                job_record.updated_at = datetime.utcnow()

                if section_heading and section_heading not in job_record.section_headings:
                    job_record.section_headings.append(section_heading)

            logger.debug(
                "Job progress updated",
                job_id=job_id,
                current_index=current_index,
                total_steps=total_steps,
                section_heading=section_heading,
            )

        return progress_callback

    def get_job_status(self, job_id: str) -> Optional[JobRecord]:
        """
        Retrieve the full job record for a given job ID.

        Thread-safe lookup of the job registry, returning the complete JobRecord
        with all status, progress, error, and result data. Returns None if the
        job ID is not found in the registry (e.g., after cleanup or invalid ID).

        Consumed by the GET /api/v1/jobs/{job_id}/status REST endpoint.

        Args:
            job_id: UUID string identifying the job to look up.

        Returns:
            The JobRecord if found, or None if the job ID is not in the registry.
        """
        with self._lock:
            return self._jobs.get(job_id)

    def get_job_progress(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve progress information for a specific job.

        Returns a focused progress dictionary containing only the fields relevant
        to progress tracking: current_index, total_steps, section_headings, and
        a computed percentage. Returns None if the job is not found.

        Consumed by the GET /api/v1/jobs/{job_id}/progress REST endpoint.

        Args:
            job_id: UUID string identifying the job to query.

        Returns:
            Dictionary with progress data including computed completion percentage,
            or None if the job ID is not in the registry.
        """
        with self._lock:
            job_record = self._jobs.get(job_id)
            if job_record is None:
                return None

            # Compute completion percentage, guarding against division by zero
            # when total_steps hasn't been set yet (early in workflow execution)
            if job_record.total_steps > 0:
                percentage = round(
                    (job_record.current_index / job_record.total_steps) * 100, 2
                )
            else:
                percentage = 0.0

            return {
                "job_id": job_record.job_id,
                "status": job_record.status.value,
                "current_index": job_record.current_index,
                "total_steps": job_record.total_steps,
                "section_headings": list(job_record.section_headings),
                "percentage": percentage,
                "updated_at": job_record.updated_at.isoformat() if job_record.updated_at else None,
            }

    def list_jobs(self, limit: int = 50) -> List[JobRecord]:
        """
        Retrieve the most recent jobs from the registry, sorted by creation time.

        Returns a list of JobRecord objects ordered by created_at descending (newest first),
        limited to the specified maximum count. Useful for administrative dashboards and
        operational monitoring.

        Args:
            limit: Maximum number of job records to return. Defaults to 50.
                  Must be a positive integer; values <= 0 are treated as 1.

        Returns:
            List of JobRecord objects sorted by created_at descending.
        """
        effective_limit = max(1, limit)

        with self._lock:
            # Sort jobs by creation timestamp descending (newest first)
            sorted_jobs = sorted(
                self._jobs.values(),
                key=lambda j: j.created_at,
                reverse=True,
            )
            return sorted_jobs[:effective_limit]

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Remove completed or failed job records older than the specified age threshold.

        Prevents unbounded memory growth in the in-memory job registry by purging
        stale records that have reached a terminal state (COMPLETE or FAILED) and
        are older than max_age_hours. Jobs in PENDING or IN_PROGRESS states are
        never removed regardless of age.

        This method should be called periodically (e.g., via a Flask CLI command,
        a scheduled background task, or at application startup).

        Args:
            max_age_hours: Maximum age in hours for terminal-state jobs before removal.
                          Defaults to 24 hours. Must be a positive integer; values <= 0
                          are treated as 1 hour.

        Returns:
            The number of job records that were removed from the registry.
        """
        effective_max_age = max(1, max_age_hours)
        cutoff_time = datetime.utcnow() - timedelta(hours=effective_max_age)
        terminal_states = {JobStatus.COMPLETE, JobStatus.FAILED}

        removed_count = 0

        with self._lock:
            # Identify stale jobs: terminal state AND created before cutoff time
            stale_job_ids = [
                job_id
                for job_id, record in self._jobs.items()
                if record.status in terminal_states and record.created_at < cutoff_time
            ]

            # Remove identified stale records
            for job_id in stale_job_ids:
                del self._jobs[job_id]
                removed_count += 1

        if removed_count > 0:
            logger.info(
                "Cleaned up old jobs",
                removed_count=removed_count,
                max_age_hours=effective_max_age,
                remaining_jobs=len(self._jobs),
            )

        return removed_count

    def shutdown(self, wait: bool = True) -> None:
        """
        Gracefully shut down the ThreadPoolExecutor.

        Should be called during Flask application teardown to ensure all background
        threads complete their work (if wait=True) or are abandoned (if wait=False).

        Args:
            wait: If True, block until all submitted jobs complete. If False,
                 cancel pending futures and return immediately. Defaults to True.
        """
        logger.info(
            "JobManager shutting down",
            wait=wait,
            active_jobs=self._get_active_job_count(),
        )
        self._executor.shutdown(wait=wait)
        logger.info("JobManager shutdown complete")

    def _get_active_job_count(self) -> int:
        """
        Count the number of jobs currently in non-terminal states.

        Returns:
            Number of jobs with status PENDING or IN_PROGRESS.
        """
        active_states = {JobStatus.PENDING, JobStatus.IN_PROGRESS}
        with self._lock:
            return sum(
                1 for record in self._jobs.values() if record.status in active_states
            )

    @property
    def active_job_count(self) -> int:
        """Property accessor for the count of active (non-terminal) jobs."""
        return self._get_active_job_count()

    @property
    def total_job_count(self) -> int:
        """Property accessor for the total number of jobs in the registry."""
        with self._lock:
            return len(self._jobs)
