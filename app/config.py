"""
Flask configuration classes for the Reverse Document Generator.

This module defines environment-specific configuration classes that map all
environment variables required by the 12+ external service integrations
(Anthropic, OpenAI, Voyage AI, Google Gemini, Neo4j, GCS, Pub/Sub,
GitHub Secret Server, Admin Service, LangSmith, and others) to Flask's
configuration system.

Configuration Hierarchy:
    Config (base) -> DevelopmentConfig | StagingConfig | ProductionConfig

Usage:
    from app.config import config_by_name

    # Select configuration by environment name
    config_class = config_by_name[os.environ.get('FLASK_ENV', 'development')]
    app.config.from_object(config_class)

All sensitive values (API keys, passwords, service URLs) are loaded exclusively
from environment variables via os.environ.get(). No credentials are hardcoded.
"""

import os


class Config:
    """
    Base configuration class for the Reverse Document Generator Flask application.

    Maps all environment variables required by the system's external service
    integrations. Each attribute is loaded from its corresponding environment
    variable using os.environ.get() with sensible defaults where appropriate.

    Attributes:
        FLASK_SECRET_KEY: Secret key for Flask session signing and CSRF protection.
        PROJECT_ID: Google Cloud Platform project identifier.
        GCS_BUCKET_NAME: Google Cloud Storage bucket for document persistence.
        PRIVATE_BLOB_NAME: GCS blob prefix for private source artifacts.
        PLATFORM_EVENTS_TOPIC: Pub/Sub topic for progress and completion notifications.
        ANTHROPIC_API_KEY: API key for Anthropic Claude Opus 4.6 LLM integration.
        OPENAI_API_KEY: API key for OpenAI GPT-5 Mini structured classification.
        VOYAGE_API_KEY: API key for Voyage AI embedding generation.
        GOOGLE_API_KEY: API key for Google Gemini (alternative Architect LLM).
        NEO4J_SERVER: Bolt protocol URI for Neo4j code graph database.
        NEO4J_USERNAME: Authentication username for Neo4j connection.
        NEO4J_PASSWORD: Authentication password for Neo4j connection.
        GITHUB_SECRET_SERVER: URL of the secret server for GitHub token retrieval.
        SERVICE_URL_ADMIN: Base URL for the Admin Service REST API.
        LANGSMITH_TRACING: Toggle for LangSmith observability tracing.
        LANGSMITH_ENDPOINT: LangSmith API endpoint URL.
        LANGSMITH_API_KEY: API key for LangSmith tracing and observability.
        LANGSMITH_PROJECT: LangSmith project identifier for trace grouping.
        TOKENIZERS_PARALLELISM: Controls HuggingFace tokenizer parallelism.
        GENERATE_REVERSE_THINKING_TOPIC: Pub/Sub topic for reverse thinking events.
    """

    # -------------------------------------------------------------------------
    # Flask Core Settings
    # -------------------------------------------------------------------------

    # Secret key for Flask session management and cryptographic signing.
    # Must be set to a strong, unique value in production environments.
    FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

    # -------------------------------------------------------------------------
    # Google Cloud Platform — Project and Infrastructure
    # -------------------------------------------------------------------------

    # GCP project identifier used across all Google Cloud service interactions
    # including GCS, Pub/Sub, and Cloud Run deployment context.
    PROJECT_ID = os.environ.get("PROJECT_ID", "blitzy-os-dev")

    # -------------------------------------------------------------------------
    # Google Cloud Storage — Document Persistence (Layer 2)
    # -------------------------------------------------------------------------

    # GCS bucket name for storing generated and updated technical specification
    # documents. Used for both incremental (per-section) and final uploads.
    GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")

    # GCS blob name prefix for private source artifacts such as document
    # prompts and existing tech specs downloaded during the setup node.
    PRIVATE_BLOB_NAME = os.environ.get("PRIVATE_BLOB_NAME", "private-src")

    # -------------------------------------------------------------------------
    # Google Cloud Pub/Sub — Outbound Notifications
    # -------------------------------------------------------------------------

    # Pub/Sub topic for publishing progress notifications (IN_PROGRESS events
    # with current_index/total_steps) and completion events (DONE with KPI
    # metadata including estimated_lines_generated and estimated_hours_saved).
    PLATFORM_EVENTS_TOPIC = os.environ.get("PLATFORM_EVENTS_TOPIC", "platform-events")

    # Pub/Sub topic for reverse thinking generation events. Used to publish
    # events related to the document generation thought process.
    GENERATE_REVERSE_THINKING_TOPIC = os.environ.get(
        "GENERATE_REVERSE_THINKING_TOPIC", ""
    )

    # -------------------------------------------------------------------------
    # AI / LLM Service API Keys
    # -------------------------------------------------------------------------

    # Anthropic API key for Claude Opus 4.6 — the primary LLM used for
    # context gathering (7+ tools), section authoring, Mermaid diagram
    # generation, and UPDATE mode targeted edits. Requires 300K token
    # context window budget.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # OpenAI API key for GPT-5 Mini — used exclusively for structured
    # classification in the identify_changes node (UPDATE mode). Classifies
    # each section as CHANGED or UNCHANGED via Pydantic structured output.
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

    # Voyage AI API key for embedding generation — powers semantic search
    # within the Neo4j code graph via the VoyageAIEmbeddings adapter.
    VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")

    # Google API key for Gemini models — serves as an alternative Architect
    # LLM when configured. Used via the langchain-google-genai adapter.
    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

    # -------------------------------------------------------------------------
    # Neo4j — Code Graph Database (Layer 1)
    # -------------------------------------------------------------------------

    # Neo4j Bolt protocol server URI (e.g., 'neo4j://host:7687'). The code
    # graph is constructed dynamically per execution via CodeGraphBuilder
    # and queried during the gather_context node for folder/file traversal.
    NEO4J_SERVER = os.environ.get("NEO4J_SERVER", "")

    # Neo4j authentication credentials. Used to establish Bolt connections
    # managed within the Flask application context lifecycle.
    NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "")
    NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

    # -------------------------------------------------------------------------
    # GitHub Integration — Repository Download
    # -------------------------------------------------------------------------

    # URL of the GitHub Secret Server used to retrieve authentication tokens
    # for repository download. The download_repository_to_disk() function
    # from blitzy_utils.scm authenticates via this endpoint.
    GITHUB_SECRET_SERVER = os.environ.get("GITHUB_SECRET_SERVER", "")

    # -------------------------------------------------------------------------
    # Admin Service — Platform API Integration
    # -------------------------------------------------------------------------

    # Base URL for the Admin Service REST API. Consumed for retrieving
    # project attachments (/v1/attachments), build information
    # (get_project_build_info), environment files
    # (download_all_environments_files), and Figma metadata
    # (get_figma_info_for_tech_spec).
    SERVICE_URL_ADMIN = os.environ.get("SERVICE_URL_ADMIN", "")

    # -------------------------------------------------------------------------
    # LangSmith — Observability and Tracing
    # -------------------------------------------------------------------------

    # Enable or disable LangSmith tracing for LLM call observability.
    # Set to 'true' to enable detailed trace collection for debugging
    # and performance analysis of the LangGraph workflow execution.
    LANGSMITH_TRACING = os.environ.get("LANGSMITH_TRACING", "true")

    # LangSmith API endpoint URL for submitting trace data.
    LANGSMITH_ENDPOINT = os.environ.get(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )

    # LangSmith API key for authenticating trace submissions.
    LANGSMITH_API_KEY = os.environ.get("LANGSMITH_API_KEY", "")

    # LangSmith project name for grouping related traces together.
    # Enables filtering and analysis of traces per deployment or feature.
    LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "")

    # -------------------------------------------------------------------------
    # Runtime Behavior — Tokenizers and Parallelism
    # -------------------------------------------------------------------------

    # Controls HuggingFace tokenizer parallelism. Set to 'false' to avoid
    # deadlocks when using tokenizers in forked processes (common with
    # Gunicorn workers). This is the recommended setting for production.
    TOKENIZERS_PARALLELISM = os.environ.get("TOKENIZERS_PARALLELISM", "false")


class DevelopmentConfig(Config):
    """
    Development environment configuration.

    Enables Flask debug mode for auto-reloading and detailed error pages.
    Sets TESTING to False to distinguish from the test environment.
    Provides a development-specific default for the GitHub Secret Server URL
    when the environment variable is not explicitly set.
    """

    # Enable debug mode for development — provides interactive debugger,
    # automatic code reloading, and detailed error tracebacks.
    DEBUG = True

    # Explicitly disable testing mode to differentiate from test fixtures
    # that may set TESTING=True for Flask test client behavior.
    TESTING = False

    # Development environment default for the GitHub Secret Server.
    # In development, this points to the dev-tier secret server instance.
    # Override via the GITHUB_SECRET_SERVER environment variable.
    GITHUB_SECRET_SERVER = os.environ.get(
        "GITHUB_SECRET_SERVER",
        "https://dev-github-secret-server.blitzy.com",
    )


class StagingConfig(Config):
    """
    Staging environment configuration.

    Disables Flask debug mode to mirror production behavior while allowing
    staging-specific environment variable values for service endpoints and
    API keys. Inherits all base Config defaults.
    """

    # Disable debug mode in staging to match production behavior and avoid
    # exposing sensitive debugging information.
    DEBUG = False


class ProductionConfig(Config):
    """
    Production environment configuration.

    Disables Flask debug mode and enforces production-grade settings.
    Provides a production-specific default for the GitHub Secret Server URL
    when the environment variable is not explicitly set. All credentials
    must be injected via environment variables in the CI/CD pipeline.
    """

    # Disable debug mode in production — never expose the interactive
    # debugger or detailed error pages in production deployments.
    DEBUG = False

    # Production environment default for the GitHub Secret Server.
    # In production, this points to the production-tier secret server instance.
    # Override via the GITHUB_SECRET_SERVER environment variable.
    GITHUB_SECRET_SERVER = os.environ.get(
        "GITHUB_SECRET_SERVER",
        "https://github-secret-server.blitzy.com",
    )


# ---------------------------------------------------------------------------
# Configuration Registry
# ---------------------------------------------------------------------------

# Maps environment name strings to their corresponding configuration classes.
# Used by the Flask application factory (create_app) to select the appropriate
# configuration based on the FLASK_ENV environment variable.
#
# Usage:
#     config_class = config_by_name.get(env_name, DevelopmentConfig)
#     app.config.from_object(config_class)
config_by_name = {
    "development": DevelopmentConfig,
    "staging": StagingConfig,
    "production": ProductionConfig,
}
