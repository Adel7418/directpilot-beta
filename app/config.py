from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}(len={len(value)})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    directpilot_mode: str = Field(default="mock", pattern="^(mock|sandbox|live_readonly)$")
    yandex_client_id: str | None = None
    yandex_client_secret: str | None = None
    yandex_oauth_token: str | None = None
    yandex_redirect_uri: str = "https://oauth.yandex.ru/verification_code"

    @property
    def is_yandex_configured(self) -> bool:
        return bool(self.yandex_client_id and self.yandex_client_secret and self.yandex_oauth_token)

    def safe_status(self) -> dict[str, str | bool | None]:
        return {
            "configured": self.is_yandex_configured,
            "client_id": mask_secret(self.yandex_client_id),
            "redirect_uri": self.yandex_redirect_uri,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
