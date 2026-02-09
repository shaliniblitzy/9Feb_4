"""
Pydantic v2 API request and response schemas for the Flask Reverse Document Generator REST API.

Defines the data contract between the REST API layer and the LangGraph workflow engine:
- GenerateRequest: 23-field payload matching the original Pub/Sub event schema for GENERATE mode
- UpdateRequest: Extends GenerateRequest with required previous_tech_spec_id for UPDATE mode
- JobStatusResponse: Status polling response for background document generation jobs
- ProgressResponse: Per-section progress tracking during workflow execution
- ErrorResponse: Structured error response format for all API error conditions
- JobStatus: Enum tracking the four lifecycle states of a background job
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Enumeration of the four lifecycle states for background document generation jobs.

    Used in JobStatusResponse and ProgressResponse to communicate the current state
    of a job through the REST API polling endpoints (GET /api/v1/jobs/{job_id}/status
    and GET /api/v1/jobs/{job_id}/progress).
    """

    PENDING = "PENDING"
    """Job has been accepted and queued but execution has not yet started."""

    IN_PROGRESS = "IN_PROGRESS"
    """Job is actively executing the LangGraph StateGraph workflow."""

    COMPLETE = "COMPLETE"
    """Job has finished successfully — generated/updated document is available in GCS."""

    FAILED = "FAILED"
    """Job encountered an unrecoverable error after exhausting all retry attempts."""


class GenerateRequest(BaseModel):
    """Request schema for the GENERATE mode document generation endpoint.

    Accepts a JSON payload equivalent to the original Pub/Sub event payload,
    containing all fields required to trigger a full document generation workflow
    via POST /api/v1/documents/generate.

    Required fields: user_id, company_id, project_id, repo_name, dest_repo_name.
    All other fields have sensible defaults matching the original event schema.
    """

    user_id: str = Field(
        ...,
        description="Unique identifier of the user initiating the document generation request.",
    )
    company_id: str = Field(
        ...,
        description="Unique identifier of the company/organization the user belongs to.",
    )
    project_id: str = Field(
        ...,
        description="Unique identifier of the Blitzy project containing the target repository.",
    )
    repo_name: str = Field(
        ...,
        description="Name of the source repository to analyze for document generation.",
    )
    repo_id: str = Field(
        default="repo_id",
        description="Unique identifier of the source repository within the platform.",
    )
    branch_id: str = Field(
        default="branch_id",
        description="Unique identifier of the source branch to analyze.",
    )
    branch_name: str = Field(
        default="main",
        description="Name of the source branch (e.g., 'main', 'develop').",
    )
    head_commit_hash: str = Field(
        default="",
        description="SHA hash of the HEAD commit on the source branch for precise versioning.",
    )
    dest_repo_name: str = Field(
        ...,
        description="Name of the destination repository where generated documents are stored.",
    )
    dest_repo_id: str = Field(
        default="repo_id",
        description="Unique identifier of the destination repository within the platform.",
    )
    dest_branch_id: str = Field(
        default="branch_id",
        description="Unique identifier of the destination branch for document output.",
    )
    dest_branch_name: str = Field(
        default="main",
        description="Name of the destination branch for document output.",
    )
    is_new_dest_repo: bool = Field(
        default=False,
        description="Flag indicating whether the destination repository is newly created.",
    )
    tech_spec_id: str = Field(
        default="",
        description="Unique identifier of the technical specification document being generated or updated.",
    )
    code_gen_id: str = Field(
        default="",
        description="Unique identifier of the associated code generation session, if any.",
    )
    document_mode: str = Field(
        default="GENERATE",
        description="Workflow mode: 'GENERATE' for full document creation, 'UPDATE' for incremental updates.",
    )
    change_mode: str = Field(
        default="ADD_FEATURE",
        description="Type of change triggering the document operation (e.g., 'ADD_FEATURE', 'BUG_FIX').",
    )
    resume: bool = Field(
        default=False,
        description="Flag to resume a previously interrupted document generation from the last completed section.",
    )
    propagate: bool = Field(
        default=True,
        description="Flag controlling whether Pub/Sub notifications are propagated to downstream consumers.",
    )
    team_id: str = Field(
        default="default",
        description="Identifier of the team within the organization initiating the request.",
    )
    org_name: str = Field(
        default="",
        description="Human-readable name of the organization for notification payloads.",
    )
    job_id: Optional[str] = Field(
        default="",
        description="Optional pre-assigned job identifier; if empty, the server generates one.",
    )
    git_project_repo_id: str = Field(
        default="",
        description="Platform-level identifier linking the Git project to the repository entity.",
    )


class UpdateRequest(GenerateRequest):
    """Request schema for the UPDATE mode incremental document update endpoint.

    Extends GenerateRequest with the required previous_tech_spec_id field that
    identifies the existing technical specification to compare against when
    performing change analysis and targeted section updates.

    Used by POST /api/v1/documents/update to trigger the UPDATE workflow path:
    summarize_changes → identify_changes → (update_section | copy_old_tech_spec_section).
    """

    previous_tech_spec_id: str = Field(
        ...,
        description=(
            "Unique identifier of the previous technical specification document "
            "to compare against for incremental change detection. Required for "
            "the UPDATE workflow to identify which sections need regeneration."
        ),
    )
    document_mode: str = Field(
        default="UPDATE",
        description="Workflow mode — defaults to 'UPDATE' for incremental document update pipeline.",
    )


class JobStatusResponse(BaseModel):
    """Response schema for the job status polling endpoint.

    Returned by GET /api/v1/jobs/{job_id}/status to communicate the current
    lifecycle state of a background document generation or update job.

    The error field is populated only when status is FAILED.
    The result field is populated only when status is COMPLETE, containing
    metadata about the generated document (e.g., GCS URL, section count).
    """

    job_id: str = Field(
        ...,
        description="Unique identifier of the background document generation job.",
    )
    status: JobStatus = Field(
        ...,
        description="Current lifecycle state of the job: PENDING, IN_PROGRESS, COMPLETE, or FAILED.",
    )
    created_at: str = Field(
        ...,
        description="ISO 8601 timestamp of when the job was created and enqueued.",
    )
    updated_at: str = Field(
        ...,
        description="ISO 8601 timestamp of the most recent status update for this job.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message populated when the job status is FAILED; null otherwise.",
    )
    result: Optional[dict] = Field(
        default=None,
        description=(
            "Result metadata populated when the job status is COMPLETE; null otherwise. "
            "Contains document URL, section count, and generation KPI metrics."
        ),
    )


class ProgressResponse(BaseModel):
    """Response schema for the job progress tracking endpoint.

    Returned by GET /api/v1/jobs/{job_id}/progress to provide granular,
    per-section progress information during active workflow execution.
    Enables clients to display real-time progress indicators for long-running
    document generation operations.
    """

    job_id: str = Field(
        ...,
        description="Unique identifier of the background document generation job.",
    )
    status: JobStatus = Field(
        ...,
        description="Current lifecycle state of the job for progress context.",
    )
    current_index: int = Field(
        ...,
        description="Zero-based index of the section currently being processed in the workflow.",
    )
    total_steps: int = Field(
        ...,
        description="Total number of sections to process in the document generation workflow.",
    )
    section_headings: List[str] = Field(
        default_factory=list,
        description="Ordered list of section heading titles being processed during workflow execution.",
    )
    percentage: float = Field(
        ...,
        description=(
            "Completion percentage (0.0 to 100.0) calculated from current_index / total_steps. "
            "Provides a normalized progress indicator for client-side display."
        ),
    )


class ErrorResponse(BaseModel):
    """Structured error response schema for all API error conditions.

    Returned by all REST endpoints when an error occurs, providing a consistent
    JSON error format with error classification, human-readable message, and
    optional additional context details.

    Used by the global Flask error handler middleware to format all error responses
    uniformly across the API surface.
    """

    error: str = Field(
        ...,
        description=(
            "Error type classification string (e.g., 'ValidationError', "
            "'NotFoundError', 'InternalServerError', 'WorkflowError')."
        ),
    )
    message: str = Field(
        ...,
        description="Human-readable error message describing what went wrong.",
    )
    details: Optional[dict] = Field(
        default=None,
        description=(
            "Optional dictionary containing additional error context such as "
            "field validation errors, stack trace identifiers, or retry information."
        ),
    )
