from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLINAGENT_", env_file=".env", extra="ignore")

    # SQLAlchemy URL for the compact FHIR store built by scripts/build_store.py.
    database_url: str = "sqlite:///data/clinical.db"
    # Optional HAPI FHIR base URL. When set, reads go to the FHIR server instead of the store.
    fhir_base_url: str | None = None

    # OpenAI-compatible chat endpoint (Ollama, vLLM, the LLM gateway, or a hosted API).
    # When unset, the agent uses its deterministic parser and template summaries only.
    llm_base_url: str | None = None
    llm_api_key: str = "not-needed"
    llm_model: str = "llama3.2"
    llm_timeout_s: float = 60.0

    # Append-only audit log (JSON lines, hash chained).
    audit_path: str = "data/audit.jsonl"

    # Recommendations below this confidence are routed to a pharmacist instead of proposed.
    min_confidence: float = 0.55
    # Approval tokens expire after this many seconds.
    approval_ttl_s: int = 900
    # HMAC key for approval tokens. Unset: a random key per process (tokens die on restart).
    approval_secret: str | None = None

    otel_exporter: str = "none"  # none | console | otlp


@lru_cache
def get_settings() -> Settings:
    return Settings()
