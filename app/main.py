from app.bootstrap.application import create_app
from app.config import RuntimeProfile, Settings, get_settings

settings = Settings()
app = create_app(
    settings=settings,
    include_legacy_router=settings.runtime_profile is RuntimeProfile.OPERATOR_LOCAL,
)

if settings.runtime_profile is RuntimeProfile.OPERATOR_LOCAL:
    from app.api.legacy_handlers import _aggregate_search_query_tsv
    from app.api.legacy_router import (
        get_yandex_client,
        get_yandex_metrika_client,
        get_yandex_search_wordstat_client,
        store,
    )

    __all__ = [
        "app",
        "_aggregate_search_query_tsv",
        "get_settings",
        "get_yandex_client",
        "get_yandex_metrika_client",
        "get_yandex_search_wordstat_client",
        "store",
    ]
else:
    __all__ = ["app"]
