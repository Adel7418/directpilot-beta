from app.api.core_router import router as core_router
from app.api.direct_extensions_router import router as direct_extensions_router
from app.api.direct_router import router as direct_router
from app.api.legacy_router import router
from app.api.metrika_router import router as metrika_router
from app.api.wordstat_router import router as wordstat_router


def test_existing_health_endpoint_is_declared_on_legacy_router() -> None:
    assert any(route.path == "/health" for route in router.routes)


def test_legacy_routes_are_grouped_by_domain_router() -> None:
    assert any(route.path == "/health" for route in core_router.routes)
    assert any(route.path == "/yandex/campaigns" for route in direct_router.routes)
    assert any(route.path == "/wordstat/top" for route in wordstat_router.routes)
    assert any(route.path == "/metrika/counters" for route in metrika_router.routes)


def _endpoint_module(router, path: str, method: str) -> str:
    route = next(
        route
        for route in router.routes
        if route.path == path and method in route.methods
    )
    return route.endpoint.__module__


def test_route_handlers_are_defined_in_bounded_domain_modules() -> None:
    assert _endpoint_module(core_router, "/health", "GET") == "app.api.core_router"
    assert _endpoint_module(direct_router, "/yandex/campaigns", "GET") == "app.api.direct_router"
    assert (
        _endpoint_module(
            direct_extensions_router,
            "/yandex/account/balance",
            "GET",
        )
        == "app.api.direct_extensions_router"
    )
    assert _endpoint_module(wordstat_router, "/wordstat/top", "GET") == "app.api.wordstat_router"
    assert _endpoint_module(metrika_router, "/metrika/counters", "GET") == "app.api.metrika_router"
