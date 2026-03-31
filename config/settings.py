from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default=f"sqlite:///{BASE_DIR}/data/estock.db",
        description="SQLAlchemy connection string. Defaults to SQLite for local dev.",
    )
    db_echo_sql: bool = Field(
        default=False,
        description="Log every SQL statement — useful for debugging, noisy in prod.",
    )

    # LLM / OpenAI
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key. Set in .env — never commit this value.",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="Model used by all agent personas.",
    )
    llm_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for agent LLM calls.",
    )
    llm_max_tokens: int = Field(
        default=1024,
        description="Token cap per agent LLM call.",
    )

    # Simulation
    simulation_facility_id: str = Field(
        default="FACILITY-001",
        description="Identifier for the facility being simulated.",
    )
    reorder_alert_threshold_days: int = Field(
        default=90,
        description="Raise a reorder alert when stock expiry is within this many days.",
    )
    low_stock_threshold_pct: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="Fraction of reorder_threshold below which a low-stock alert fires.",
    )
    anomaly_spike_multiplier: float = Field(
        default=3.0,
        description="Daily consumption must exceed this multiple of the 30-day average to be flagged.",
    )

    # API
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    api_reload: bool = Field(default=True)

    # Paths
    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @property
    def drugs_catalog_path(self) -> Path:
        return self.data_dir / "drugs_catalog.json"

    @property
    def seed_stock_path(self) -> Path:
        return self.data_dir / "seed_stock.json"


settings = Settings()
