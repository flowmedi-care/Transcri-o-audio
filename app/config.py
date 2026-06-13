from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(validation_alias="API_KEY")

    host: str = Field(default="127.0.0.1", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")

    whisper_model: str = Field(default="small", validation_alias="WHISPER_MODEL")
    whisper_compute_type: str = Field(default="int8", validation_alias="WHISPER_COMPUTE_TYPE")
    whisper_language: str = Field(default="pt", validation_alias="WHISPER_LANGUAGE")

    max_file_size_mb: int = Field(default=50, validation_alias="MAX_FILE_SIZE_MB")
    max_duration_minutes: int = Field(default=60, validation_alias="MAX_DURATION_MINUTES")
    max_processing_minutes: int = Field(default=45, validation_alias="MAX_PROCESSING_MINUTES")
    max_jobs_per_minute: int = Field(default=10, validation_alias="MAX_JOBS_PER_MINUTE")
    max_concurrent_jobs: int = Field(default=1, validation_alias="MAX_CONCURRENT_JOBS")

    queue_db_path: Path = Field(default=Path("./data/queue.db"), validation_alias="QUEUE_DB_PATH")
    temp_dir: Path = Field(default=Path("./data/tmp"), validation_alias="TEMP_DIR")

    supabase_url: str = Field(default="", validation_alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", validation_alias="SUPABASE_SERVICE_KEY")

    save_audio: bool = Field(default=False, validation_alias="SAVE_AUDIO")
    save_transcript: bool = Field(default=False, validation_alias="SAVE_TRANSCRIPT")
    save_metrics: bool = Field(default=True, validation_alias="SAVE_METRICS")

    dev_ui_enabled: bool = Field(default=True, validation_alias="DEV_UI_ENABLED")

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def max_duration_seconds(self) -> float:
        return self.max_duration_minutes * 60

    @property
    def max_processing_seconds(self) -> float:
        return self.max_processing_minutes * 60

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
