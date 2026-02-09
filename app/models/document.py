"""
Core Pydantic v2 data models for document sections in the Reverse Document Generator.

This module defines the foundational data models used throughout the LangGraph
workflow engine for representing, classifying, and tracking document sections
during both GENERATE and UPDATE modes:

- DocumentSectionStatus: Enum for section classification (CHANGED/UNCHANGED)
  used by GPT-5 Mini in the identify_changes node during UPDATE mode.
- DocumentSection: Individual tech spec section with heading, content, and
  classification status. Used in both GENERATE mode (Author Agent populates
  heading + content) and UPDATE mode (status drives routing in document_router).
- DocumentSections: Ordered collection of DocumentSection instances, used as
  the structured output format for GPT-5 Mini's with_structured_output() call
  in the identify_changes node for deterministic LLM output parsing.

These models are consumed by:
  - identify_changes node: GPT-5 Mini structured classification output
  - update_section node: Processes sections with status == CHANGED
  - copy_old_tech_spec_section node: Preserves sections with status == UNCHANGED
  - document_section node: Section authoring in GENERATE mode
  - document_router: Routes based on section classification status
"""

from enum import Enum
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class DocumentSectionStatus(str, Enum):
    """Classification status for document sections during UPDATE mode.

    Used by GPT-5 Mini structured classification in the identify_changes node
    to categorize each section of the existing tech spec. The classification
    determines whether a section needs regeneration or can be copied verbatim:

    - CHANGED: Section content is outdated relative to the current codebase
      and requires regeneration via the update_section node.
    - UNCHANGED: Section content is still accurate and can be copied verbatim
      from the old tech spec via the copy_old_tech_spec_section node.

    This enum inherits from both str and Enum to ensure that its values
    serialize naturally to JSON strings, which is required for compatibility
    with Pydantic v2 model serialization and LLM structured output parsing.
    """

    CHANGED = "CHANGED"
    """Section requires regeneration in UPDATE mode — routed to update_section node."""

    UNCHANGED = "UNCHANGED"
    """Section can be copied verbatim from old tech spec — routed to copy_old_tech_spec_section node."""


class DocumentSection(BaseModel):
    """Represents an individual section of a technical specification document.

    This model is the fundamental unit of document structure used throughout
    the LangGraph workflow. It serves dual purposes across the two execution modes:

    GENERATE mode:
        - The Author Agent populates heading and content for each section.
        - Status is not used (defaults to UNCHANGED).
        - Sections are created sequentially by the document_section node.

    UPDATE mode:
        - The identify_changes node (GPT-5 Mini) classifies each existing
          section as CHANGED or UNCHANGED.
        - The document_router reads the status field to route:
          * CHANGED sections → update_section node for targeted regeneration
          * UNCHANGED sections → copy_old_tech_spec_section for verbatim copy
        - Content is either regenerated or preserved based on classification.

    Attributes:
        heading: The section heading text (e.g., "## 1. Executive Summary").
        content: The full markdown content of the section body. Defaults to
            empty string for newly created sections awaiting content generation.
        status: Classification status for UPDATE mode routing. Defaults to
            UNCHANGED, indicating no changes detected until GPT-5 Mini
            classifies otherwise.
    """

    model_config = ConfigDict(use_enum_values=True)
    """Pydantic v2 model configuration enabling JSON serialization compatibility.

    use_enum_values=True ensures that DocumentSectionStatus enum members are
    serialized as their string values ("CHANGED"/"UNCHANGED") rather than as
    Enum objects, which is required for:
    - Correct JSON serialization in API responses
    - Compatibility with LLM structured output parsing
    - Clean dict representation via model_dump()
    """

    heading: str = Field(
        description="The section heading text of the tech spec section, "
        "typically a Markdown heading (e.g., '## 1. Executive Summary')"
    )

    content: str = Field(
        default="",
        description="The full markdown content of the section body. "
        "Empty string by default for sections awaiting content generation "
        "by the Author Agent in GENERATE mode or targeted update in UPDATE mode.",
    )

    status: DocumentSectionStatus = Field(
        default=DocumentSectionStatus.UNCHANGED,
        description="Classification status for UPDATE mode — CHANGED sections "
        "are routed to the update_section node for targeted regeneration, "
        "while UNCHANGED sections are routed to the copy_old_tech_spec_section "
        "node for verbatim preservation from the previous tech spec.",
    )


class DocumentSections(BaseModel):
    """Ordered collection of all document sections with their content and status.

    This model is the structured output format used by GPT-5 Mini's
    `with_structured_output(DocumentSections)` call in the identify_changes
    node. It enables deterministic parsing of the LLM's section classification
    output, ensuring that every section of the existing tech spec is properly
    categorized as CHANGED or UNCHANGED.

    The sections list preserves the original document ordering, which is
    critical for maintaining structural consistency when sections are
    individually updated or copied during the UPDATE mode pipeline.

    Usage in the LangGraph workflow:
        - identify_changes node: GPT-5 Mini returns DocumentSections as
          structured output, with each section classified by status.
        - The workflow iterates over sections, using section_index to track
          progress and document_router to route each section based on status.

    Attributes:
        sections: Ordered list of DocumentSection instances representing
            every section of the tech spec document. Empty by default,
            populated by the identify_changes node during UPDATE mode
            or by sequential document_section calls during GENERATE mode.
    """

    sections: List[DocumentSection] = Field(
        default_factory=list,
        description="Ordered list of all document sections with their content "
        "and classification status. Populated by GPT-5 Mini structured output "
        "in identify_changes (UPDATE mode) or sequentially by the Author Agent "
        "in document_section (GENERATE mode).",
    )
