"""Central configuration. Every value can be overridden with an environment
variable (or a line in backend/.env) of the same name in UPPER_CASE."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- input -------------------------------------------------------------
    log_file: Path = PROJECT_DIR / "data" / "app.log"
    log_format: str = "generic"          # generic | bgl
    tail_from_end: bool = True           # skip history already in the file at startup
    tail_poll_interval: float = 0.25     # seconds between file checks

    # ---- sliding window ---------------------------------------------------
    window_seconds: float = 60.0         # size of the rolling window
    tick_seconds: float = 5.0            # how often a snapshot is evaluated
    min_events_per_window: int = 20      # fewer events => low-confidence snapshot

    # ---- baseline -----------------------------------------------------------
    baseline_alpha: float = 0.05         # EWMA smoothing factor
    baseline_warmup_ticks: int = 24      # 24 x 5s = 2 minutes of silent learning
    baseline_min_std: float = 0.01       # std floor so z-scores stay sane

    # ---- severity -----------------------------------------------------------
    # Every detector is normalised so 1.0 == "edge of normal" (z=3 / p99).
    sev_low: float = 1.0
    sev_medium: float = 1.5
    sev_high: float = 2.5
    sev_critical: float = 4.0
    critical_error_rate: float = 0.5     # absolute override
    escalate_after_windows: int = 3      # persistent anomaly escalates one level

    # ---- alerting -----------------------------------------------------------
    alert_cooldown_seconds: float = 60.0
    resolve_after_normal_windows: int = 3
    incident_gap_seconds: float = 120.0

    # ---- ML -----------------------------------------------------------------
    artifacts_dir: Path = BACKEND_DIR / "artifacts"
    enable_ml: bool = True
    sequence_length: int = 10            # windows per LSTM-AE sequence
    weight_zscore: float = 1.0
    weight_iforest: float = 1.0
    weight_lstm: float = 1.0

    # ---- storage ------------------------------------------------------------
    db_path: Path = BACKEND_DIR / "alerts.db"
    fallback_alert_file: Path = PROJECT_DIR / "data" / "alerts_fallback.jsonl"

    # ---- AWS ----------------------------------------------------------------
    aws_enabled: bool = False
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # e.g. http://localhost:4566 for LocalStack
    cloudwatch_log_group: str = "/log-anomaly-detector/alerts"
    cloudwatch_log_stream: str = "alerts"
    sns_topic_arn: str | None = None
    sns_min_severity: str = "HIGH"       # only page people for HIGH+
    publish_retries: int = 3

    # ---- built-in simulator / demo -----------------------------------------
    simulator_enabled: bool = False      # generate logs inside the backend (one-command demo)
    simulator_rate: float = 20.0         # lines/sec, match the training data
    inject_control_file: Path = PROJECT_DIR / "data" / "inject_request.json"

    # ---- API ----------------------------------------------------------------
    cors_origins: str = "*"
    enable_demo_endpoints: bool = True

    @field_validator("aws_endpoint_url", "sns_topic_arn", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return v or None

    @field_validator("log_file", "db_path", "artifacts_dir", "fallback_alert_file",
                     "inject_control_file", mode="after")
    @classmethod
    def _relative_to_backend(cls, v: Path) -> Path:
        # relative paths in .env are resolved from backend/, whatever the cwd
        return v if v.is_absolute() else (BACKEND_DIR / v).resolve()


settings = Settings()
