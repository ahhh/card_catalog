from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_path: str = "data/catalog.db"
    scryfall_user_agent: str = "card_catalog/0.1 (personal collection tracker)"
    scryfall_delay_ms: int = 100
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def templates_dir(self) -> Path:
        return self.project_root / "templates"

    @property
    def static_dir(self) -> Path:
        return self.project_root / "static"

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"


settings = Settings()
