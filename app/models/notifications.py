"""
Pydantic v2 models for Pub/Sub notification payloads.

Defines the three-tier notification lifecycle models used by the Reverse Document
Generator's PubSubService:

  1. IN_PROGRESS (initial)  — signals workflow start
  2. IN_PROGRESS (per-section) — reports current_index / total_steps progress
  3. DONE                    — delivers KPI metadata upon completion
  4. FAILED                  — reports error details on workflow failure

All models inherit from BaseNotification which carries the common identity and
routing fields (projectId, jobId, user_id, repo_id, phase, status, metadata).
Each subclass adds status-specific payload fields and overrides ``to_pubsub_dict()``
for serialisation into the flat dictionary format expected by
``PubSubService.publish_notification()``.

These models ensure consistent, type-safe payload formatting across every
Pub/Sub publish operation in the application.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Notification metadata (nested within every notification payload)
# ---------------------------------------------------------------------------

class NotificationMetadata(BaseModel):
    """Metadata block embedded in every Pub/Sub notification.

    Attributes:
        propagate: Whether downstream consumers should propagate this event
                   further through the platform event bus.  Defaults to True.
        repo_name: The repository name associated with the current document
                   generation or update job.
        extra:     Catch-all for any additional metadata key/value pairs that
                   may be attached by callers (e.g. custom tags, trace IDs).
    """

    propagate: bool = Field(
        default=True,
        description=(
            "Flag indicating whether downstream consumers should propagate "
            "this notification further through the platform event bus."
        ),
    )
    repo_name: str = Field(
        ...,
        description="Repository name associated with the current job.",
    )
    extra: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Additional optional metadata key/value pairs. Merged into the "
            "top-level metadata dict during serialisation."
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise metadata to a plain dictionary.

        The ``extra`` entries are merged into the top-level dict so that
        consumers see a flat metadata structure.

        Returns:
            A dictionary suitable for embedding in the Pub/Sub message payload.
        """
        base: Dict[str, Any] = {
            "propagate": self.propagate,
            "repo_name": self.repo_name,
        }
        if self.extra:
            base.update(self.extra)
        return base


# ---------------------------------------------------------------------------
# Base notification — shared fields across IN_PROGRESS, DONE, and FAILED
# ---------------------------------------------------------------------------

class BaseNotification(BaseModel):
    """Common fields carried by every Pub/Sub notification event.

    This model mirrors the ``notification_data`` dictionary structure
    originally constructed in the batch job's ``main.py`` and provides a
    type-safe, validated representation of the Pub/Sub payload.

    Attributes:
        projectId:            Platform project identifier (maps to project_id).
        jobId:                Unique job identifier (maps to job_id).
        tech_spec_id:         Tech-spec document identifier.
        code_gen_id:          Code-generation session identifier.
        org_name:             Organisation name for the owning team.
        repo_id:              Repository identifier.
        branch_name:          Git branch targeted by this job.
        phase:                Workflow phase constant — ``"REVERSE_DOCUMENT"``
                              for the Reverse Document Generator.
        status:               Notification status — ``"IN_PROGRESS"``,
                              ``"DONE"``, or ``"FAILED"``.
        user_id:              Identifier of the user who initiated the job.
        git_project_repo_id:  Git-project-repo mapping identifier.
        metadata:             Nested metadata (propagate flag, repo_name, extras).
    """

    projectId: str = Field(
        ...,
        description="Platform project identifier (maps to project_id).",
    )
    jobId: str = Field(
        ...,
        description="Unique job identifier (maps to job_id).",
    )
    tech_spec_id: str = Field(
        default="",
        description="Tech-spec document identifier.",
    )
    code_gen_id: str = Field(
        default="",
        description="Code-generation session identifier.",
    )
    org_name: str = Field(
        default="",
        description="Organisation name for the owning team.",
    )
    repo_id: str = Field(
        ...,
        description="Repository identifier.",
    )
    branch_name: str = Field(
        default="main",
        description="Git branch targeted by this job.",
    )
    phase: str = Field(
        default="REVERSE_DOCUMENT",
        description=(
            "Workflow phase constant. For the Reverse Document Generator this "
            "is always 'REVERSE_DOCUMENT' (equivalent to "
            "ProjectPhase.FILE_MAPPING.value in the platform enum)."
        ),
    )
    status: str = Field(
        ...,
        description=(
            "Notification status — one of 'IN_PROGRESS', 'DONE', or 'FAILED'."
        ),
    )
    user_id: str = Field(
        ...,
        description="Identifier of the user who initiated the job.",
    )
    git_project_repo_id: str = Field(
        default="",
        description="Git-project-repo mapping identifier.",
    )
    metadata: NotificationMetadata = Field(
        ...,
        description=(
            "Nested metadata block containing the propagate flag, repo_name, "
            "and any extra key/value pairs."
        ),
    )

    def to_pubsub_dict(self) -> Dict[str, Any]:
        """Serialise this notification to the dictionary format expected by
        ``PubSubService.publish_notification()``.

        The method produces a flat dictionary where the ``metadata`` field is
        serialised via :py:meth:`NotificationMetadata.to_dict` and all
        ``None`` values from optional subclass fields are excluded so that
        Pub/Sub consumers receive only the fields that are actually populated.

        Returns:
            A ``Dict[str, Any]`` ready to be JSON-encoded and published to
            the platform events Pub/Sub topic.
        """
        data: Dict[str, Any] = self.model_dump(exclude_none=True)
        # Replace the nested Pydantic metadata model with its plain dict form
        # to ensure a clean serialisation for Pub/Sub.
        data["metadata"] = self.metadata.to_dict()
        return data


# ---------------------------------------------------------------------------
# IN_PROGRESS notification (initial and per-section variants)
# ---------------------------------------------------------------------------

class InProgressNotification(BaseNotification):
    """Pub/Sub payload for IN_PROGRESS notifications.

    Two variants exist within this single model:

    * **Initial progress** — ``current_index`` and ``total_steps`` are
      ``None``; signals that the workflow has started.
    * **Per-section progress** — ``current_index`` and ``total_steps`` are
      populated; reports incremental progress through the document sections.

    Attributes:
        status:           Defaults to ``"IN_PROGRESS"``.
        current_index:    Zero-based index of the section currently being
                          processed.  ``None`` for the initial notification.
        total_steps:      Total number of sections in the document.  ``None``
                          for the initial notification.
        section_heading:  Heading text of the section currently being
                          processed.  ``None`` for the initial notification.
    """

    status: str = Field(
        default="IN_PROGRESS",
        description="Notification status — always 'IN_PROGRESS' for this type.",
    )
    current_index: Optional[int] = Field(
        default=None,
        description=(
            "Zero-based index of the section currently being processed. "
            "Populated for per-section progress updates; None for the initial "
            "IN_PROGRESS notification."
        ),
    )
    total_steps: Optional[int] = Field(
        default=None,
        description=(
            "Total number of sections in the document. Populated for "
            "per-section progress updates; None for the initial notification."
        ),
    )
    section_heading: Optional[str] = Field(
        default=None,
        description=(
            "Heading text of the section currently being processed. "
            "Populated for per-section progress updates; None for the initial "
            "notification."
        ),
    )

    def to_pubsub_dict(self) -> Dict[str, Any]:
        """Serialise IN_PROGRESS notification to Pub/Sub dict format.

        Extends the base serialisation by including ``current_index``,
        ``total_steps``, and ``section_heading`` only when they are set
        (i.e. for per-section progress events).  The initial IN_PROGRESS
        notification omits these fields entirely.

        Returns:
            A ``Dict[str, Any]`` ready for Pub/Sub publishing.
        """
        data = super().to_pubsub_dict()
        return data


# ---------------------------------------------------------------------------
# DONE notification (completion with KPI metadata)
# ---------------------------------------------------------------------------

class DoneNotification(BaseNotification):
    """Pub/Sub payload for DONE notifications emitted on workflow completion.

    Carries KPI metadata that downstream consumers (e.g. the Admin Service
    dashboard) use for analytics and reporting.

    Attributes:
        status:                     Defaults to ``"DONE"``.
        estimated_lines_generated:  Approximate number of Markdown lines
                                    produced by the document generation run.
        estimated_hours_saved:      Estimated human-hours saved by automated
                                    documentation generation.
        document_url:               GCS URL of the completed document.
        total_sections_processed:   Count of sections processed during the run.
    """

    status: str = Field(
        default="DONE",
        description="Notification status — always 'DONE' for this type.",
    )
    estimated_lines_generated: Optional[int] = Field(
        default=None,
        description=(
            "Approximate number of Markdown lines produced during document "
            "generation. Serves as a KPI metric for analytics."
        ),
    )
    estimated_hours_saved: Optional[float] = Field(
        default=None,
        description=(
            "Estimated human-hours saved by this automated documentation run. "
            "Serves as a KPI metric for analytics."
        ),
    )
    document_url: Optional[str] = Field(
        default=None,
        description="GCS URL of the completed document.",
    )
    total_sections_processed: Optional[int] = Field(
        default=None,
        description=(
            "Total count of sections processed during the document "
            "generation or update run."
        ),
    )

    def to_pubsub_dict(self) -> Dict[str, Any]:
        """Serialise DONE notification to Pub/Sub dict format.

        Extends the base serialisation by including KPI metadata fields
        (``estimated_lines_generated``, ``estimated_hours_saved``,
        ``document_url``, ``total_sections_processed``) only when they
        are populated.

        Returns:
            A ``Dict[str, Any]`` ready for Pub/Sub publishing.
        """
        data = super().to_pubsub_dict()
        return data


# ---------------------------------------------------------------------------
# FAILED notification (error reporting)
# ---------------------------------------------------------------------------

class FailedNotification(BaseNotification):
    """Pub/Sub payload for FAILED notifications emitted on workflow failure.

    Provides structured error information so that downstream consumers can
    classify, log, and potentially trigger remediation actions.

    Attributes:
        status:        Defaults to ``"FAILED"``.
        error_message: Human-readable description of the failure.
        error_type:    Programmatic classification of the error (e.g.
                       exception class name).
    """

    status: str = Field(
        default="FAILED",
        description="Notification status — always 'FAILED' for this type.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Human-readable description of the failure.",
    )
    error_type: Optional[str] = Field(
        default=None,
        description=(
            "Programmatic classification of the error — typically the "
            "exception class name (e.g. 'AnthropicAPIError', 'Neo4jError')."
        ),
    )

    def to_pubsub_dict(self) -> Dict[str, Any]:
        """Serialise FAILED notification to Pub/Sub dict format.

        Extends the base serialisation by including ``error_message`` and
        ``error_type`` when they are populated.

        Returns:
            A ``Dict[str, Any]`` ready for Pub/Sub publishing.
        """
        data = super().to_pubsub_dict()
        return data
