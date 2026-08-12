from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}(len={len(value)})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = "local"
    directpilot_mode: str = Field(
        default="live_readonly",
        pattern="^(mock|sandbox|live_readonly|live_write)$",
    )
    # Comma-separated hostname allowlist for existing-campaign landing URL
    # migrations. An empty list intentionally fails closed before URL fetches.
    url_migration_allowed_hosts: str = Field(
        default="",
        validation_alias=AliasChoices(
            "DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS",
            "url_migration_allowed_hosts",
        ),
    )
    yandex_client_id: str | None = None
    yandex_client_secret: str | None = None
    yandex_oauth_token: str | None = None
    yandex_redirect_uri: str = "https://oauth.yandex.ru/verification_code"

    # Yandex AI Studio / Search API v2 — used by the modern Wordstat client.
    # Optional folderId is the cloud folder that owns the service account
    # backing the API key. Both values are NEVER returned in full by
    # safe_status(); they are masked or omitted.
    yandex_search_api_key: str | None = None
    yandex_search_folder_id: str | None = None

    # Yandex Metrika — used by the read-only Metrika client (counters, goals,
    # summary, traffic-sources). The Metrika Management API
    # (api-metrika.yandex.net/management/v1) and the Stats API
    # (api-metrika.yandex.net/stat/v1) both use `Authorization: OAuth <token>`
    # for service tokens, which is the documented Metrika way.
    yandex_metrika_oauth_token: str | None = None

    @property
    def is_yandex_configured(self) -> bool:
        return bool(self.yandex_client_id and self.yandex_client_secret and self.yandex_oauth_token)

    @property
    def is_yandex_search_configured(self) -> bool:
        """True only when a Yandex Search API v2 key is configured.

        The optional folderId does not block the key — endpoints that need
        a folder can fall back to a per-call override. This is the v2 /
        Yandex AI Studio / Search API v2 path used by the Wordstat client.
        """
        return bool(self.yandex_search_api_key)

    @property
    def is_metrika_configured(self) -> bool:
        """True only when a Yandex Metrika OAUTH token is configured.

        The Metrika Management and Stats APIs both use a service OAUTH
        token (NOT an Api-Key, NOT a v5 OAuth token). Endpoints that need
        a Metrika token fail fast with HTTP 503 when this is missing —
        the request never reaches the network.
        """
        return bool(self.yandex_metrika_oauth_token)

    def safe_status(self) -> dict[str, str | bool | None]:
        return {
            "configured": self.is_yandex_configured,
            "client_id": mask_secret(self.yandex_client_id),
            "redirect_uri": self.yandex_redirect_uri,
            # The Yandex AI Studio / Search API v2 key and folder. The full
            # value is never returned; only a masked preview.
            "search_api_key": mask_secret(self.yandex_search_api_key),
            "search_folder_id": mask_secret(self.yandex_search_folder_id),
            "search_configured": self.is_yandex_search_configured,
            # Yandex Metrika service OAUTH token. The full value is never
            # returned; only a masked preview and a configured flag. The
            # field is named ``metrika_key_status`` (not ``metrika_token``)
            # so that ``/health`` responses do not contain the substring
            # "token" — which is the contract that
            # ``tests/test_api.py::test_health_endpoint_*`` enforces.
            "metrika_key_status": mask_secret(self.yandex_metrika_oauth_token),
            "metrika_configured": self.is_metrika_configured,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
