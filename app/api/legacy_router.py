from fastapi import APIRouter

from app.api.core_router import router as core_router
from app.api.direct_extensions_router import router as direct_extensions_router
from app.api.direct_router import router as direct_router
from app.api.legacy_handlers import (
    get_settings,
    get_yandex_client,
    get_yandex_metrika_client,
    get_yandex_search_wordstat_client,
    store,
)
from app.api.metrika_router import router as metrika_router
from app.api.wordstat_router import router as wordstat_router

__all__ = [
    "get_settings",
    "get_yandex_client",
    "get_yandex_metrika_client",
    "get_yandex_search_wordstat_client",
    "router",
    "store",
]

router = APIRouter()
router.include_router(core_router)
router.include_router(direct_router)
router.include_router(wordstat_router)
router.include_router(direct_extensions_router)
router.include_router(metrika_router)
