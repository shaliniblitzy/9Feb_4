"""
Admin Service integration client for the Reverse Document Generator Flask application.

Wraps the Blitzy platform's ServiceClient REST API client for consuming 4 Admin Service
endpoints, and the get_figma_info_for_tech_spec utility function for Figma metadata
retrieval. This module is the single point of integration between the LangGraph workflow
engine and the Admin Service backend.

Endpoints Consumed:
    1. **POST /v1/attachments** — Downloads user-uploaded attachments (images, documents)
       for inclusion in LLM context prompts during document generation (Feature F-006).
    2. **get_project_build_info()** — Retrieves project build metadata (framework, language,
       infrastructure details) used in the setup node for project context (Feature F-011).
    3. **download_all_environments_files()** — Downloads environment configuration files
       for the project, used during context gathering (Feature F-011).
    4. **get_figma_info_for_tech_spec()** — Retrieves Figma design asset metadata including
       availability status, attachment list, and Personal Access Token (PAT) used to
       determine if the MCP bridge should be initialized (Feature F-005).

Error Handling Strategy:
    - Endpoints 1-3: Each call is wrapped in try/except. Errors are logged with full
      context and re-raised to allow callers (workflow nodes) to handle via the
      @archie_exponential_retry() decorator.
    - Endpoint 4 (get_figma_info): Returns a safe default dict on failure for graceful
      degradation per AAP §0.7.4, since Figma integration is conditional and its
      absence should never abort document generation.

Exports:
    AdminService: Service class with Flask app factory pattern (init_app).

Dependencies:
    - blitzy_utils.service_client.ServiceClient — REST API client for Admin Service.
    - blitzy_utils.figma.get_figma_info_for_tech_spec — Figma metadata retrieval.
    - blitzy_utils.logger.BlitzyLogger — Structured logging factory.
"""

from typing import Any, Dict, List, Optional

from blitzy_utils.figma import get_figma_info_for_tech_spec
from blitzy_utils.logger import BlitzyLogger
from blitzy_utils.service_client import ServiceClient

# ---------------------------------------------------------------------------
# Module-level structured logger for Admin Service operations.
# Logs include: initialization status, endpoint call details (project/tech_spec IDs),
# successful responses, and error conditions with full exception context.
# ---------------------------------------------------------------------------
logger = BlitzyLogger(__name__)

# ---------------------------------------------------------------------------
# Safe default response for Figma info retrieval failures.
# Returned when get_figma_info_for_tech_spec() raises any exception, ensuring
# the workflow continues without Figma integration rather than aborting.
# This implements the graceful degradation requirement from AAP §0.7.4:
#   "Figma integration must skip gracefully when not available"
# ---------------------------------------------------------------------------
_FIGMA_INFO_SAFE_DEFAULT: Dict[str, Any] = {
    "is_available": False,
    "attachments": [],
    "pat": "",
}


class AdminService:
    """
    Service class wrapping the Admin Service REST API and Figma metadata retrieval.

    Implements the Flask extension pattern with lazy initialization via ``init_app()``.
    The ServiceClient instance is created once per application lifecycle and reused
    across all requests, matching the application-scoped singleton injection model
    documented in AAP §0.4.1.

    Usage::

        # In Flask application factory (app/__init__.py):
        admin_service = AdminService()
        admin_service.init_app(app)

        # In workflow nodes:
        attachments = admin_service.get_attachments(attachment_ids)
        build_info = admin_service.get_project_build_info(project_id)
        env_files = admin_service.download_all_environments_files(project_id)
        figma_info = admin_service.get_figma_info(tech_spec_id)

    Attributes:
        service_client: ServiceClient instance for Admin Service HTTP calls.
            Initialized in ``init_app()`` with the SERVICE_URL_ADMIN base URL.
        service_url: Base URL of the Admin Service REST API loaded from Flask
            config (SERVICE_URL_ADMIN environment variable).
    """

    def __init__(self, app: Optional[Any] = None) -> None:
        """
        Initialize the AdminService instance.

        Creates the instance with uninitialized state. If a Flask app is provided,
        immediately calls ``init_app()`` to complete initialization. Otherwise,
        initialization is deferred until ``init_app()`` is called explicitly
        (standard Flask extension lazy initialization pattern).

        Args:
            app: Optional Flask application instance. When provided, ``init_app()``
                is called immediately to configure the service with the app's
                configuration. Pass ``None`` for deferred initialization.
        """
        self.service_client: Optional[ServiceClient] = None
        self.service_url: str = ""

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Any) -> None:
        """
        Initialize the AdminService with a Flask application's configuration.

        Loads the Admin Service base URL from the Flask app config and creates
        the ServiceClient instance used for all subsequent HTTP operations.
        This method is idempotent — calling it multiple times reconfigures
        the service with the latest app configuration.

        The SERVICE_URL_ADMIN config value maps to the environment variable of the
        same name, configured in ``app/config.py``. Example value:
        ``https://admin.blitzy-platform.internal/api``

        Args:
            app: Flask application instance whose ``config`` dict contains
                the ``SERVICE_URL_ADMIN`` key.

        Raises:
            KeyError: If ``SERVICE_URL_ADMIN`` is not present in app.config
                and no default is available (handled gracefully with empty string).
        """
        self.service_url = app.config.get("SERVICE_URL_ADMIN", "")

        # Initialize the ServiceClient with the Admin Service base URL.
        # ServiceClient provides .get() and .post() methods for HTTP operations
        # with built-in timeout handling and response parsing.
        if self.service_url:
            self.service_client = ServiceClient(self.service_url)
            logger.info(
                "AdminService initialized successfully",
                service_url_configured=True,
            )
        else:
            # Allow initialization without a URL for testing or local development
            # where the Admin Service may not be available. Endpoint calls will
            # raise descriptive errors if attempted without configuration.
            self.service_client = None
            logger.warning(
                "AdminService initialized without SERVICE_URL_ADMIN — "
                "Admin Service endpoints will be unavailable"
            )

    def get_attachments(self, attachment_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Download user-uploaded attachments from the Admin Service.

        Consumes the ``POST /v1/attachments`` endpoint to retrieve attachment
        metadata and content for the specified attachment IDs. The returned
        attachment data is subsequently processed by the attachment_processor
        module for Base64 encoding and LLM context inclusion.

        This method supports Feature F-006 (Attachment Processing and Multi-Modal
        Input) by providing the raw attachment data that feeds into the
        ``download_attachment_as_base64()`` pipeline.

        Args:
            attachment_ids: List of unique attachment identifier strings to
                retrieve. These IDs originate from the request payload's
                attachment references (e.g., Figma screenshots, user-uploaded
                images, documents).

        Returns:
            List of dictionaries, each containing attachment metadata and content
            data. Each dict typically includes keys like ``id``, ``url``,
            ``filename``, ``content_type``, and ``data``. Returns an empty list
            if no attachment IDs are provided.

        Raises:
            RuntimeError: If the AdminService has not been initialized with
                a valid SERVICE_URL_ADMIN (service_client is None).
            Exception: Propagates any HTTP or network errors from the
                ServiceClient to allow retry handling by the caller's
                @archie_exponential_retry() decorator.
        """
        if not attachment_ids:
            logger.debug("get_attachments called with empty attachment_ids list")
            return []

        if self.service_client is None:
            raise RuntimeError(
                "AdminService is not initialized. Call init_app() with a Flask app "
                "that has SERVICE_URL_ADMIN configured before calling get_attachments()."
            )

        try:
            logger.info(
                "Fetching attachments from Admin Service",
                attachment_count=len(attachment_ids),
            )

            # POST the attachment IDs to /v1/attachments to retrieve their data.
            # The Admin Service returns a list of attachment objects matching
            # the requested IDs.
            response = self.service_client.post(
                "/v1/attachments",
                json={"attachment_ids": attachment_ids},
            )

            # Normalize response — ensure we always return a list even if the
            # Admin Service returns a dict wrapper or unexpected format.
            if isinstance(response, list):
                attachments = response
            elif isinstance(response, dict) and "attachments" in response:
                attachments = response["attachments"]
            elif isinstance(response, dict) and "data" in response:
                attachments = response["data"]
            else:
                # Fallback: wrap the response in a list if it's a single object
                attachments = [response] if response else []

            logger.info(
                "Successfully fetched attachments",
                requested=len(attachment_ids),
                received=len(attachments),
            )
            return attachments

        except Exception as exc:
            logger.error(
                "Failed to fetch attachments from Admin Service",
                attachment_ids=attachment_ids,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_project_build_info(self, project_id: str) -> Dict[str, Any]:
        """
        Retrieve project build metadata from the Admin Service.

        Fetches framework, programming language, infrastructure, and build
        configuration details for the specified project. This metadata is
        consumed by the setup workflow node to provide project context for
        the AI agents during document generation.

        This method supports Feature F-011 (Project Environment and Build
        Information Integration) by supplying the project's technology stack
        information that informs documentation structure and content.

        Args:
            project_id: Unique identifier of the project whose build
                information is being requested.

        Returns:
            Dictionary containing project build metadata. Typical keys include
            ``framework``, ``language``, ``infrastructure``, ``build_tool``,
            ``deployment_target``, and other project-specific configuration
            details. Returns an empty dict if the project has no build info.

        Raises:
            RuntimeError: If the AdminService has not been initialized with
                a valid SERVICE_URL_ADMIN (service_client is None).
            Exception: Propagates any HTTP or network errors from the
                ServiceClient to allow retry handling by the caller.
        """
        if self.service_client is None:
            raise RuntimeError(
                "AdminService is not initialized. Call init_app() with a Flask app "
                "that has SERVICE_URL_ADMIN configured before calling "
                "get_project_build_info()."
            )

        try:
            logger.info(
                "Fetching project build info from Admin Service",
                project_id=project_id,
            )

            # Retrieve build info via GET request with project_id as path parameter.
            # The ServiceClient.get() method handles URL construction, HTTP execution,
            # and response deserialization.
            response = self.service_client.get(
                f"/v1/projects/{project_id}/build-info",
            )

            # Normalize response to dict
            build_info = response if isinstance(response, dict) else {}

            logger.info(
                "Successfully fetched project build info",
                project_id=project_id,
                keys_returned=list(build_info.keys()) if build_info else [],
            )
            return build_info

        except Exception as exc:
            logger.error(
                "Failed to fetch project build info from Admin Service",
                project_id=project_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def download_all_environments_files(
        self, project_id: str
    ) -> Dict[str, Any]:
        """
        Download all environment configuration files for a project.

        Retrieves environment-specific configuration files (e.g., .env files,
        deployment configs, infrastructure-as-code templates) from the Admin
        Service. These files provide additional project context for the AI
        agents during document generation, enabling accurate documentation
        of environment setup and deployment procedures.

        This method supports Feature F-011 (Project Environment and Build
        Information Integration) by providing environment configuration data
        that informs the technical specification's infrastructure sections.

        Args:
            project_id: Unique identifier of the project whose environment
                files are being requested.

        Returns:
            Dictionary containing environment file data. Typical structure
            maps environment names to their respective configuration data,
            e.g., ``{"production": {...}, "staging": {...}}``. Returns an
            empty dict if no environment files exist for the project.

        Raises:
            RuntimeError: If the AdminService has not been initialized with
                a valid SERVICE_URL_ADMIN (service_client is None).
            Exception: Propagates any HTTP or network errors from the
                ServiceClient to allow retry handling by the caller.
        """
        if self.service_client is None:
            raise RuntimeError(
                "AdminService is not initialized. Call init_app() with a Flask app "
                "that has SERVICE_URL_ADMIN configured before calling "
                "download_all_environments_files()."
            )

        try:
            logger.info(
                "Downloading environment files from Admin Service",
                project_id=project_id,
            )

            # Retrieve all environment configuration files for the project.
            # The Admin Service returns a dictionary mapping environment names
            # to their respective configuration data.
            response = self.service_client.get(
                f"/v1/projects/{project_id}/environments",
            )

            # Normalize response to dict
            env_files = response if isinstance(response, dict) else {}

            logger.info(
                "Successfully downloaded environment files",
                project_id=project_id,
                environments_count=len(env_files),
            )
            return env_files

        except Exception as exc:
            logger.error(
                "Failed to download environment files from Admin Service",
                project_id=project_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    def get_figma_info(self, tech_spec_id: str) -> Dict[str, Any]:
        """
        Retrieve Figma design asset metadata for a technical specification.

        Wraps the ``get_figma_info_for_tech_spec()`` utility function from
        ``blitzy_utils.figma`` to retrieve Figma availability status, design
        asset attachments, and the Personal Access Token (PAT) required to
        initialize the MCP bridge for Figma integration.

        The returned data drives the conditional Figma MCP bridge initialization
        decision in the workflow setup::

            figma_info = admin_service.get_figma_info(tech_spec_id)
            is_figma_available = (
                figma_info["is_available"]
                and len(figma_info["attachments"]) > 0
            )

        **Graceful Degradation**: Unlike the other 3 endpoints, this method
        returns a safe default on failure (``is_available=False``, empty
        attachments, empty PAT) instead of raising an exception. This ensures
        document generation proceeds without Figma integration when the Figma
        metadata service is unavailable, per AAP §0.7.4.

        This method supports Feature F-005 (Design Asset Integration) by
        providing the metadata needed to determine whether Figma design
        assets should be incorporated into the generated documentation.

        Args:
            tech_spec_id: Unique identifier of the technical specification
                whose Figma design assets are being queried.

        Returns:
            Dictionary with the following keys:
                - ``is_available`` (bool): Whether Figma integration is available
                  for this tech spec. True only if the project has linked Figma
                  files and a valid PAT is configured.
                - ``attachments`` (list): List of Figma design asset references
                  (file keys, node IDs, page names) to be processed by the
                  MCP bridge.
                - ``pat`` (str): Figma Personal Access Token for API authentication.
                  Empty string if not configured.

            On any error, returns the safe default:
            ``{"is_available": False, "attachments": [], "pat": ""}``
        """
        try:
            logger.info(
                "Fetching Figma info for tech spec",
                tech_spec_id=tech_spec_id,
            )

            # Call the blitzy_utils.figma utility function directly.
            # This function communicates with the Admin Service backend to
            # retrieve Figma-specific metadata for the given tech spec.
            figma_info = get_figma_info_for_tech_spec(
                tech_spec_id=tech_spec_id,
            )

            # Validate and normalize the response structure.
            # Ensure all expected keys are present with correct types.
            if not isinstance(figma_info, dict):
                logger.warning(
                    "get_figma_info_for_tech_spec returned non-dict response, "
                    "using safe default",
                    tech_spec_id=tech_spec_id,
                    response_type=type(figma_info).__name__,
                )
                return dict(_FIGMA_INFO_SAFE_DEFAULT)

            # Ensure required keys exist with sensible defaults
            normalized_info: Dict[str, Any] = {
                "is_available": figma_info.get("is_available", False),
                "attachments": figma_info.get("attachments", []),
                "pat": figma_info.get("pat", ""),
            }

            # Determine effective Figma availability for logging
            effective_available = (
                normalized_info["is_available"]
                and len(normalized_info["attachments"]) > 0
            )

            logger.info(
                "Successfully fetched Figma info",
                tech_spec_id=tech_spec_id,
                is_available=normalized_info["is_available"],
                attachment_count=len(normalized_info["attachments"]),
                effective_available=effective_available,
            )
            return normalized_info

        except Exception as exc:
            # Graceful degradation: return safe default instead of propagating
            # the exception. Figma integration is conditional — its failure
            # must never abort document generation.
            logger.warning(
                "Failed to fetch Figma info — returning safe default for "
                "graceful degradation",
                tech_spec_id=tech_spec_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return dict(_FIGMA_INFO_SAFE_DEFAULT)
