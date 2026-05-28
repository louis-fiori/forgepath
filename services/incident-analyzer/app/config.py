"""Runtime configuration, all overridable by environment variable.

Secrets come from a mounted Kubernetes Secret; everything else defaults in the
Deployment manifest.
"""

from __future__ import annotations

import os
from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # --- HTTP server ---
    port: int = 8080

    # --- What to watch & how often ---
    # Comma-separated namespaces to poll.
    watch_namespaces: str = "incident-generator"
    # Background auto-detection loop. False = analyze only on demand via /analyze.
    poll_enabled: bool = True
    poll_interval_seconds: int = 60
    detect_window: str = "10m"  # LogQL range for the error-count query

    # --- Detection thresholds ---
    error_threshold: int = 20  # error lines in the window → incident
    panic_threshold: int = 1  # increase in recovered panics → incident
    refile_cooldown_seconds: int = 6 * 60 * 60  # 6h: don't re-file the same fingerprint

    # --- Observability backends (in-cluster, no auth) ---
    loki_url: str = "http://loki.loki.svc.cluster.local:3100"
    prometheus_url: str = "http://prometheus.prometheus.svc.cluster.local:9090"

    # --- LLM ---
    # Provider for the Claude call: "bedrock" (AWS, SigV4) or "anthropic" (direct API key).
    llm_provider: str = "bedrock"
    max_tokens: int = 2048
    # Per-call ceiling; the poller is sequential so a hung request would freeze
    # detection. Kept under poll_interval_seconds (60s).
    llm_timeout_seconds: float = 45.0
    # Retries on transient errors, capped low so an outage can't stack timeouts
    # and stall the sequential loop.
    llm_max_retries: int = 1

    # Direct Anthropic API path.
    anthropic_api_key: str = ""
    analyzer_model: str = "claude-sonnet-4-6"

    # Amazon Bedrock path. Model IDs carry the `anthropic.` prefix, often a
    # region-scoped inference profile. Two auth modes, access key first: (1) static
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN for STS), no
    # ~/.aws needed; (2) AWS_PROFILE from a mounted ~/.aws. Access key wins.
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6"
    aws_region: str = "us-east-1"
    aws_profile: str = "default"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    # --- Data masking ---
    # Best-effort redaction of sensitive shapes from log samples and pod events
    # before they reach the LLM, keeping PII inside the trust boundary.
    masking_enabled: bool = True

    # --- Surfacing: GitHub issue ---
    # Default for opening issues; overridable via POST /settings or the
    # `open_issue` param on /analyze.
    issues_enabled: bool = True
    github_token: str = ""
    github_owner: str = "louis-fiori"
    github_repo: str = "forgepath"
    github_branch: str = "dev"  # branch to read runbooks from (raw.githubusercontent)

    # --- Surfacing: Backstage notification ---
    backstage_url: str = "http://backstage.backstage.svc.cluster.local:7007"
    backstage_s2s_token: str = ""

    # Base URL for the dashboard links rendered into issues and notifications.
    # Browser-facing, so it points at the externally reachable Grafana, not the
    # in-cluster service address.
    grafana_url: str = "http://localhost:3000"

    @property
    def namespaces(self) -> list[str]:
        return [n.strip() for n in self.watch_namespaces.split(",") if n.strip()]

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)

    @property
    def backstage_enabled(self) -> bool:
        return bool(self.backstage_s2s_token)

    @property
    def auth_enabled(self) -> bool:
        """Whether the side-effecting endpoints require the s2s bearer token.
        Reuses BACKSTAGE_S2S_TOKEN, so enforcement turns on wherever it's set;
        empty token means open (local / poll-only dev)."""
        return bool(self.backstage_s2s_token)

    @cached_property
    def llm_enabled(self) -> bool:
        # cached_property, not property: the poller reads this every cycle and the
        # bedrock branch hits the disk (os.path.exists). The inputs (env vars,
        # mounted creds) are fixed for the pod's lifetime, so resolve once. Tests
        # override the whole attribute, so the cache never masks a fixture change.
        if self.llm_provider == "bedrock":
            # Static access key, else a resolvable profile (mounted or ~/.aws).
            if (self.aws_access_key_id and self.aws_secret_access_key) or os.environ.get("AWS_ACCESS_KEY_ID"):
                return True
            cred = os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.expanduser("~/.aws/credentials")
            conf = os.environ.get("AWS_CONFIG_FILE") or os.path.expanduser("~/.aws/config")
            return os.path.exists(cred) or os.path.exists(conf)
        return bool(self.anthropic_api_key)

    @property
    def effective_model(self) -> str:
        return self.bedrock_model_id if self.llm_provider == "bedrock" else self.analyzer_model


settings = Settings()
