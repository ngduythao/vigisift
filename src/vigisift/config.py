from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The vision model everything defaults to: Frigate's summaries, the rule re-check, the local
# assistant and the offline eval. One copy loaded in Ollama serves them all. The Frigate
# template (deploy/frigate/config.template.yml) and .env.example name it too: change all three.
DEFAULT_VLM_MODEL = "qwen3-vl:8b-instruct"


class FrigateCredential(BaseModel):
    """The Frigate account VigiSift uses to read reviews and push camera rules.

    Pushing rules (`PUT /api/config/set`) needs Frigate's admin role; viewer is
    enough for only reading reviews.
    """

    user: str = Field(min_length=1)
    password: SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    database_url: str = Field(min_length=1)
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = Field(default=1883, gt=0)
    mqtt_username: str | None = "vigisift"
    mqtt_password: str | None = "vigisift"
    # TLS to the broker (port 8883, `make certs`). `mqtt_ca_file` is the CA to trust: empty uses
    # the system's CAs, as with a broker holding a public certificate.
    mqtt_tls: bool = False
    mqtt_ca_file: str | None = None
    # A command sent to a device must arrive, but the device can lose power midway;
    # past this deadline the command counts as missed instead of hanging forever.
    command_timeout_sec: int = Field(default=30, gt=0)
    command_sweep_sec: float = Field(default=5, gt=0)

    # Daily ceiling on paid LLM tokens (UTC). Past it the assistant refuses to answer
    # rather than burning more money.
    llm_daily_token_budget: int = Field(default=500_000, gt=0)

    # Chat assistant (agent + function calling). `ollama` runs the model locally
    # with no per-token cost; `anthropic` needs ANTHROPIC_API_KEY.
    assistant_enabled: bool = False
    assistant_provider: Literal["ollama", "anthropic"] = "ollama"
    assistant_model: str = "claude-opus-5"

    # Local model through Ollama for the assistant: no per-token cost. Same model Frigate uses
    # for summaries, so Ollama keeps only one model loaded. The `qwen3-vl:8b` tag is the Thinking
    # build: slow, and another 6 GB to download.
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = DEFAULT_VLM_MODEL

    # Re-check alerts against the camera rules (features/rules/check.py), through Ollama
    # at `ollama_host`. Uses EXACTLY the model and num_ctx Frigate summarises with: the
    # same copy already loaded in Ollama, with no second model loaded.
    rule_check_enabled: bool = True
    rule_check_model: str = DEFAULT_VLM_MODEL
    rule_check_num_ctx: int = Field(default=16384, ge=2048)
    # Like Frigate's `keep_alive`: how long Ollama holds the model while unused.
    rule_check_keep_alive: str = Field(default="10m", pattern=r"^(-1|\d+[smh])$")
    rule_check_interval_sec: float = Field(default=5, gt=0)
    rule_check_timeout_sec: float = Field(default=60, gt=0)

    # Reconcile with each site's Frigate API, to recover reviews MQTT missed.
    frigate_reconcile_interval_sec: float = Field(default=300, ge=10)
    # Read back this much before the reconciled mark: a review that ended or got its
    # summary after the previous pass is read again.
    frigate_reconcile_overlap_sec: float = Field(default=900, ge=0)
    # A site never reconciled before reads back this far.
    frigate_initial_lookback_sec: float = Field(default=24 * 3600, gt=0)
    frigate_timeout_sec: float = Field(default=10, gt=0)
    # The CA for sites whose Frigate serves HTTPS with a private certificate (`make certs` writes
    # deploy/certs/ca.crt). Empty trusts the system's CAs.
    frigate_ca_file: str | None = None
    # Push camera rules Frigate is missing or changed (features/rules/sync.py).
    rule_sync_interval_sec: float = Field(default=300, ge=10)
    # JSON by site id, for example {"bakery-store": {"user": "vigisift", "password": "..."}}.
    # A site not listed here is called without sign-in (internal port 5000 only).
    frigate_credentials: dict[str, FrigateCredential] = Field(default_factory=dict)

    # Operator session: expires after being idle too long (long enough for a shift),
    # and even while active doesn't live past `session_max_age_sec`.
    session_ttl_sec: int = Field(default=12 * 3600, gt=0)
    session_max_age_sec: int = Field(default=24 * 3600, gt=0)
    # Send the cookie over HTTPS only. Turn off only for dev over http://127.0.0.1 (compose does).
    session_cookie_secure: bool = True
    # The address operators open VigiSift at, for example https://vigisift.example.com. Set it
    # when running behind a reverse proxy: writes and the WebSocket must carry exactly this Origin.
    # Empty compares Origin with Host (enough for dev, not when a proxy changes Host).
    public_origin: str | None = None
    # The WebSocket re-checks the session this often: signing out stops the event stream.
    ws_session_check_sec: float = Field(default=30, gt=0)
    # This many failures in the window locks sign-in for that username (for an IP: 4 times this).
    login_max_failures: int = Field(default=5, gt=0)
    login_window_sec: int = Field(default=15 * 60, gt=0)

    redis_url: str = "redis://127.0.0.1:6380/0"
    events_channel: str = "vigisift:events"
    # No heartbeat within this deadline counts as offline, even if the Last Will
    # hasn't fired (device software hung while the socket stays open).
    presence_ttl_sec: float = Field(default=30, gt=0)

    # How long data is kept (features/retention/job.py, runs once a day). Alerts describe
    # people, so they aren't kept forever: Frigate already drops the clip after 7 days.
    # Unreviewed alerts, LLM call records, telemetry and finished commands go after
    # `retention_days` (the longest metrics and costs window is 90 days). Alerts an operator
    # judged are eval labels: kept `reviewed_retention_days` (label export reaches back 365).
    retention_days: int = Field(default=90, ge=1)
    reviewed_retention_days: int = Field(default=365, ge=1)

    @field_validator("public_origin", "mqtt_ca_file", "frigate_ca_file", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # compose passes `${PUBLIC_ORIGIN:-}` as an empty string when it isn't set.
        return None if value == "" else value

    @model_validator(mode="after")
    def _reviewed_kept_at_least_as_long(self) -> "Settings":
        if self.reviewed_retention_days < self.retention_days:
            raise ValueError("REVIEWED_RETENTION_DAYS must be at least RETENTION_DAYS")
        return self


def load_settings() -> Settings:
    return Settings()
