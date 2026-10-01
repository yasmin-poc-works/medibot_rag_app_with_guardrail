"""Application configuration loaded from the environment.

The module intentionally supports both ``uv run --env-file .env`` and
``python-dotenv`` so local development behaves consistently in every shell.
"""

from functools import lru_cache
from pathlib import Path
import tempfile

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseSettings):
    """Strongly typed runtime settings for the MediBot API."""

    groq_api_key: str = Field(validation_alias="GROQ_API_KEY")
    groq_model: str = Field(validation_alias="GROQ_MODEL")
    qdrant_url: str = Field(validation_alias="QDRANT_URL")
    qdrant_collection: str = Field(validation_alias="QDRANT_COLLECTION")
    qdrant_path: str = Field(default="", validation_alias="QDRANT_PATH")
    embed_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        validation_alias="EMBED_MODEL",
    )
    sparse_model: str = Field(default="Qdrant/bm25", validation_alias="SPARSE_MODEL")
    reranker_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        validation_alias="RERANKER_MODEL",
    )
    database_path: str = Field(validation_alias="DATABASE_PATH")
    frontend_origin: str = Field(validation_alias="FRONTEND_ORIGIN")

    # Evaluation & guardrail layer. The judge is deliberately a different
    # (larger) Groq model than GROQ_MODEL so the system never grades itself.
    judge_model: str = Field(default="openai/gpt-oss-120b", validation_alias="JUDGE_MODEL")
    # gpt-oss reasoning effort for the judge (low/medium/high, or empty to omit).
    # "low" keeps a full evaluation run inside Groq's free-tier daily token quota.
    judge_reasoning_effort: str = Field(default="low", validation_alias="JUDGE_REASONING_EFFORT")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", validation_alias="GROQ_BASE_URL")
    guardrail_llm_enabled: bool = Field(default=True, validation_alias="GUARDRAIL_LLM_ENABLED")
    log_dir: str = Field(default="logs", validation_alias="LOG_DIR")
    latency_threshold_ms: int = Field(default=20000, validation_alias="LATENCY_THRESHOLD_MS")

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def log_path(self) -> Path:
        """Return the structured-log directory resolved relative to the backend folder."""

        path = Path(self.log_dir)
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def database_file(self) -> Path:
        """Return the database path resolved relative to the backend folder."""

        database = Path(self.database_path)
        return database if database.is_absolute() else ROOT_DIR / database

    @property
    def local_qdrant_path(self) -> str:
        """Return the notebook-style local Qdrant path.

        ``QDRANT_PATH`` takes precedence. Otherwise a temporary path is used,
        which keeps the demo self-contained and avoids requiring a Qdrant
        server. ``QDRANT_URL`` remains available for a future server-backed
        adapter.
        """

        if self.qdrant_path:
            return self.qdrant_path
        return str(Path(tempfile.gettempdir()) / "mediassist_qdrant")


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings object for dependency injection and tests."""

    return Settings()
