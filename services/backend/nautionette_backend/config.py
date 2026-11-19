"""Configuration. Everything comes from the environment, nothing from a file."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.temporal_address = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
        self.temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        self.temporal_task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "nautionette")
        self.temporal_history_retention_days = int(os.environ.get("TEMPORAL_HISTORY_RETENTION_DAYS", "7"))
        if not 1 <= self.temporal_history_retention_days <= 36500:
            raise ValueError("TEMPORAL_HISTORY_RETENTION_DAYS must be between 1 and 36500")

        self.gateway_url = os.environ.get("AGENTGATEWAY_URL", "http://agentgateway:4000")
        # Private helper alongside agentgateway; never derive this from a browser URL.
        gateway = urlsplit(self.gateway_url)
        host = f"[{gateway.hostname}]" if ":" in (gateway.hostname or "") else gateway.hostname
        self.gateway_cli_oauth_url = os.environ.get(
            "AGENTGATEWAY_CLI_OAUTH_URL", urlunsplit((gateway.scheme, f"{host}:4001", "", "", ""))
        ).rstrip("/")
        self.llm_base_url = os.environ.get("LLM_BASE_URL", f"{self.gateway_url}/v1")
        self.mcp_url = os.environ.get("MCP_URL", f"{self.gateway_url}/mcp")
        self.backend_mcp_url = os.environ.get("BACKEND_MCP_URL", "http://backend:8080/mcp/")
        self.broker_url = os.environ.get("BROKER_URL", "http://docker-broker:9100")
        self.workflow_mcp_url = os.environ.get("WORKFLOW_MCP_URL", "http://workflow-mcp:8000")
        self.frontend_web_url = os.environ.get("FRONTEND_WEB_URL", "http://frontend-web:80")

        self.workflows_dir = os.environ.get("WORKFLOWS_DIR", "/workflows")
        self.data_dir = os.environ.get("DATA_DIR", "/data")
        self.artifacts_dir = os.environ.get("ARTIFACTS_DIR", "/data/artifacts")

        # Server-only S3 credentials. Agent tools receive only narrowly scoped,
        # short-lived binary transfer URLs, never these values.
        self.output_s3_endpoint = os.environ.get("OUTPUT_S3_ENDPOINT", "http://maxio:9000").strip()
        self.output_s3_bucket = os.environ.get("OUTPUT_S3_BUCKET", "nautionette-outputs").strip()
        self.output_s3_region = os.environ.get("OUTPUT_S3_REGION", "us-east-1").strip()
        self.output_s3_access_key = os.environ.get("OUTPUT_S3_ACCESS_KEY", "").strip()
        self.output_s3_secret_key = os.environ.get("OUTPUT_S3_SECRET_KEY", "").strip()
        # Read-only mount of MaxIO's actual backing volume, not the container's
        # overlay filesystem. Leave empty for an independently hosted S3 service.
        self.output_storage_disk_path = os.environ.get("OUTPUT_STORAGE_DISK_PATH", "").strip()
        self.public_url = os.environ.get("APP_URL", "").strip().rstrip("/")

        # CPU-only local embeddings; Docker bundles the model for offline startup.
        self.search_semantic_enabled = _flag("SEARCH_SEMANTIC_ENABLED", True)
        self.search_model_cache = os.environ.get("SEARCH_MODEL_CACHE", f"{self.data_dir}/search-model")
        self.search_model_local_only = _flag("SEARCH_MODEL_LOCAL_ONLY", False)

        # APP_TOKEN is retained for migration diagnostics only, never human authentication.
        self.app_token = os.environ.get("APP_TOKEN", "").strip()
        self.internal_token = os.environ.get("INTERNAL_TOKEN", "").strip()
        if self.app_token and self.internal_token == self.app_token:
            raise ValueError("INTERNAL_TOKEN must differ from the obsolete APP_TOKEN; use separate secrets")
        self.public_url = os.environ.get("DENKZEUG_PUBLIC_URL", self.public_url).strip().rstrip("/")
        self.bootstrap_username = os.environ.get("DENKZEUG_BOOTSTRAP_ADMIN_USERNAME", "").strip()
        self.bootstrap_email = os.environ.get("DENKZEUG_BOOTSTRAP_ADMIN_EMAIL", "").strip()
        self.bootstrap_password_file = os.environ.get("DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD_FILE", "").strip()
        self.bootstrap_password = os.environ.get("DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD", "")
        self.smtp_host = os.environ.get("DENKZEUG_SMTP_HOST", "").strip()
        self.smtp_port = int(os.environ.get("DENKZEUG_SMTP_PORT", "587"))
        self.smtp_username = os.environ.get("DENKZEUG_SMTP_USERNAME", "").strip()
        self.smtp_password_file = os.environ.get("DENKZEUG_SMTP_PASSWORD_FILE", "").strip()
        self.smtp_password = os.environ.get("DENKZEUG_SMTP_PASSWORD", "")
        self.smtp_from = os.environ.get("DENKZEUG_SMTP_FROM", "").strip()
        self.smtp_tls_mode = os.environ.get("DENKZEUG_SMTP_TLS_MODE", "starttls").strip().lower()

        # Interactive turns use the broker's wall-clock ceiling. Workflow calls
        # keep their separate, explicit per-activity timeout (default 900s).
        self.agent_run_timeout_seconds = max(1, int(os.environ.get("AGENT_RUN_TIMEOUT_SECONDS", "3600")))
        # Keep HTTP/MCP serving during this bounded chat drain. The orchestrator
        # must allow additional time for connection shutdown and agent cleanup.
        self.backend_chat_drain_seconds = max(0, int(os.environ.get("BACKEND_CHAT_DRAIN_SECONDS", "3600")))
        self.default_agent_set = os.environ.get("DEFAULT_AGENT_SET", "default")
        self.agent_model = os.environ.get("AGENT_MODEL", "openai/gpt-4o-mini")
        self.model_key_present = bool(os.environ.get("MODEL_KEY_PRESENT", "").strip())
        self.public_demo = _flag("PUBLIC_DEMO", False)
        # Keep the raw revision for telemetry/deployed-commit verification.
        self.version = os.environ.get("APP_VERSION", "dev")
        self.build_commit = self.version if re.fullmatch(r"[0-9a-f]{40}", self.version) else None
        # Optional deployment intent, never inferred from Git tags or environment.
        release_tag = os.environ.get("APP_RELEASE_TAG", "")
        self.release_tag = (
            release_tag
            if len(release_tag) <= 64
            and re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", release_tag)
            else None
        )
        self.environment = os.environ.get("APP_ENVIRONMENT", "production").strip().lower()
        if self.environment not in {"production", "staging", "development"}:
            raise ValueError("APP_ENVIRONMENT must be production, staging, or development")

        self.auth_allow_insecure_localhost = _flag("DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST", False)
        if self.auth_allow_insecure_localhost and self.environment != "development":
            raise ValueError("Insecure account sessions are restricted to APP_ENVIRONMENT=development")
        if self.smtp_tls_mode not in {"starttls", "ssl", "plain"}:
            raise ValueError("DENKZEUG_SMTP_TLS_MODE must be starttls, ssl or plain")
        if self.smtp_tls_mode == "plain" and self.environment != "development":
            raise ValueError("Unencrypted SMTP is restricted to development")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("Invalid DENKZEUG_SMTP_PORT")
        if self.public_url:
            public = urlsplit(self.public_url)
            local = public.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not public.hostname
                or public.username
                or public.password
                or public.query
                or public.fragment
                or public.path not in {"", "/"}
                or (
                    public.scheme != "https"
                    and not (public.scheme == "http" and local and self.auth_allow_insecure_localhost)
                )
            ):
                raise ValueError("DENKZEUG_PUBLIC_URL must be a canonical HTTPS origin")

        # Origins the packaged app is served from. Add your own with CORS_ORIGINS.
        self.cors_origins = [
            "capacitor://localhost",
            "http://localhost",
            "https://localhost",
            *[origin.strip() for origin in os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()],
        ]

    @property
    def auth_enabled(self) -> bool:
        return True


settings = Settings()
