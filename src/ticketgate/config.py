"""Settings, read from the environment or a local `.env` file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # fake runs everything offline. bedrock calls AWS with the default credential chain.
    llm_mode: Literal["fake", "bedrock"] = "fake"
    # Cross-region inference profile ids, from the Bedrock model cards.
    bedrock_model_id: str = "us.anthropic.claude-sonnet-5-5"
    bedrock_worker_model_id: str = "us.anthropic.claude-haiku-5-5"
    aws_region: str = "us-east-1"

    # demo replays examples/demo_tickets.json and records write-backs in a local file.
    ticket_source: Literal["demo", "github"] = "demo"
    demo_tickets_path: Path = REPO_ROOT / "examples" / "demo_tickets.json"

    github_repo: str | None = None
    github_token: SecretStr | None = None
    # Only ingest issues with this label. Worth setting on a public repository, where anyone
    # can open an issue and so spend your model budget.
    github_label_filter: str | None = None
    max_tickets_per_poll: int = 50

    jira_enabled: bool = False
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_jql: str | None = None

    target_repo_path: Path = REPO_ROOT / "examples" / "demo-target"
    max_investigation_steps: int = 12
    data_dir: Path = REPO_ROOT / "data"

    # JSON object of reviewer name to token. Unset means local development: there is no
    # login and reviewers type their own name, so "who" in the audit log is self-reported.
    reviewer_tokens: SecretStr | None = None
    # How often to poll sources. 0 means only when someone asks.
    poll_interval_seconds: int = 0
    worker_threads: int = 2
    web_dist_path: Path = REPO_ROOT / "web" / "dist"
