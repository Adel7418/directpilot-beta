from app.api.legacy_handlers import _aggregate_search_query_tsv
from app.api.legacy_router import (
    get_settings,
    get_yandex_client,
    get_yandex_metrika_client,
    get_yandex_search_wordstat_client,
    router,
    store,
)
from app.bootstrap.application import create_app

__all__ = [
    "app",
    "_aggregate_search_query_tsv",
    "get_settings",
    "get_yandex_client",
    "get_yandex_metrika_client",
    "get_yandex_search_wordstat_client",
    "store",
]

app = create_app()
app.include_router(router)
