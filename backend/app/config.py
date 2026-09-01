from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///:memory:"
    llm_base_url: str = "http://llm.invalid/v1"
    llm_api_key: str = "test-key"
    llm_model: str = "test-model"
    llm_max_concurrency: int = 4
    search_provider: str = "none"
    fetch_user_agent: str = "os-news-tracker/0.1 (+internal)"
    fetch_per_host_delay_seconds: float = 2.0
    missing_date_policy: str = "include_as_now"
    manual_fetch_max_workers: int = 4
    wechat_mp_cookie: str | None = None
    wechat_mp_token: str | None = None
    wechat_mp_profile_name: str = "wechat_mp_default"
    wechat_browser_data_dir: str = "/var/lib/os-news-tracker/wechat-browser"
    wechat_qr_session_timeout_seconds: int = 300
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str = "no-reply@example.com"
    smtp_from_name: str = "OS News Tracker"
    smtp_use_tls: bool = False
    smtp_use_ssl: bool = False
    mail_provider: str = "tof4"
    tof4_paasid: str | None = None
    tof4_token: str | None = None
    tof4_url: str | None = None
    tof4_from_email: str | None = None
    tof4_from_name: str | None = None
    system_access_password: str = "admin"
    discovery_max_concurrent_runs: int = 3
    discovery_sandbox_max_containers: int = 4
    discovery_sandbox_runtime: str = "runsc"
    discovery_sandbox_platform: str = "systrap"
    discovery_sandbox_runsc_binary: str = "/usr/local/bin/runsc"
    discovery_sandbox_runsc_version: str = ""
    discovery_sandbox_docker_daemon_config: str = "/etc/docker/daemon.json"
    discovery_sandbox_cpus: float = 1.0
    discovery_sandbox_memory: str = "512m"
    discovery_agent_sandbox_memory: str = "1g"
    discovery_agent_sandbox_pids_limit: int = Field(default=256, ge=128, le=512)
    discovery_sandbox_pids_limit: int = 64
    discovery_sandbox_timeout_seconds: float = 120.0
    discovery_sandbox_runtime_images: dict[str, str] = Field(default_factory=dict)
    discovery_sandbox_docker_command: str = "docker"
    discovery_sandbox_docker_socket: str = "/var/run/docker.sock"
    discovery_sandbox_proxy_port: int = 8080
    discovery_sandbox_attestation_keyring_path: str = "/var/lib/os-news-tracker/discovery-attestation/keyring.json"
    discovery_sandbox_allow_ephemeral_attestation_keyring: bool = False
    discovery_checkpoint_root: str = "/var/lib/os-news-tracker/discovery-checkpoints"
    discovery_checkpoint_max_bytes: int = 4 * 1024 * 1024
    discovery_workspace_root: str = "/var/lib/os-news-tracker/discovery-workspaces"
    discovery_connector_root: str = "/var/lib/os-news-tracker/connectors"
    discovery_sandbox_capacity_lock_root: str = "/var/lib/os-news-tracker/connectors/.sandbox-capacity"
    discovery_runtime_version: str = "crawler-runtime:3"
    discovery_agent_runtime_version: str = "openhands-agent-runtime:1.43.1-r2"
    discovery_agent_max_llm_requests: int = 80
    discovery_agent_max_llm_body_bytes: int = 2 * 1024 * 1024
    discovery_agent_max_llm_response_bytes: int = 8 * 1024 * 1024
    discovery_loop_max_seconds: float = 1800.0
    discovery_rag_top_k: int = 5
    discussion_mail_enabled: bool = False
    discussion_mail_cron: str = "0 8 * * *"
    discussion_auto_organize: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
