from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    b2_key_id: str = ""
    b2_application_key: str = ""
    b2_bucket: str = ""
    b2_s3_endpoint: str = ""

    google_service_account_file: str = "./secrets/service-account.json"
    gdrive_export_formats: str = "docx,xlsx,pptx"

    rclone_transfers: int = 8
    rclone_checkers: int = 16
    max_delete_threshold: int = 500
    alert_webhook_url: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "backup.db"


settings = Settings()
