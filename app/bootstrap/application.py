from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bootstrap.dependencies import (
    ApplicationDependencies,
    _fake_auth_is_requested,
    create_application_dependencies,
)
from app.bootstrap.public_profile import (
    validate_public_profile_dependencies,
    validate_public_profile_static,
)
from app.config import RuntimeProfile, Settings
from app.core.errors import install_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.modules.sessions.middleware import (
    CookieCsrfMiddleware,
    SessionWorkspaceContextMiddleware,
)


class _ApplicationRuntimeCloser:
    def __init__(self, dependencies: ApplicationDependencies) -> None:
        self._runtime = dependencies.database_runtime
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._runtime is not None:
            self._runtime.close()


@asynccontextmanager
async def _application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        app.state.runtime_closer.close()


def _validate_explicit_dependencies(
    *,
    settings: Settings,
    dependencies: ApplicationDependencies,
    include_legacy_router: bool,
) -> None:
    validate_public_profile_static(
        settings,
        include_legacy_router=include_legacy_router,
        fake_auth_enabled=dependencies.fake_auth_enabled or _fake_auth_is_requested(),
        database_configured=dependencies.database_runtime is not None,
    )
    validate_public_profile_dependencies(settings, dependencies)


def create_app(
    *,
    settings: Settings | None = None,
    dependencies: ApplicationDependencies | None = None,
    include_legacy_router: bool = False,
) -> FastAPI:
    """Create the DirectPilot ASGI application for the selected runtime profile."""

    settings = settings if settings is not None else Settings()
    explicit_dependencies = dependencies is not None
    if dependencies is None:
        dependencies = create_application_dependencies(
            settings,
            include_legacy_router=include_legacy_router,
        )

    closer = _ApplicationRuntimeCloser(dependencies)
    try:
        if explicit_dependencies:
            _validate_explicit_dependencies(
                settings=settings,
                dependencies=dependencies,
                include_legacy_router=include_legacy_router,
            )

        is_public = settings.runtime_profile is RuntimeProfile.PUBLIC
        app = FastAPI(
            title="DirectPilot Beta API",
            version="0.2.1",
            description="Standalone API-first beta app for safe Yandex Direct automation.",
            docs_url=None if is_public else "/docs",
            redoc_url=None if is_public else "/redoc",
            openapi_url=None if is_public else "/openapi.json",
            lifespan=_application_lifespan,
        )
        app.state.dependencies = dependencies
        app.state.runtime_closer = closer
        app.add_middleware(SessionWorkspaceContextMiddleware)
        app.add_middleware(RequestContextMiddleware)
        app.add_middleware(CookieCsrfMiddleware)
        app.add_middleware(SecurityHeadersMiddleware)

        if is_public:
            from app.api.public_runtime_router import router as public_runtime_router
            from app.modules.integrations.yandex.router import (
                router as yandex_oauth_router,
            )

            app.include_router(public_runtime_router)
            app.include_router(yandex_oauth_router)
        else:
            from app.modules.identity.router import router as identity_router
            from app.modules.integrations.yandex.router import (
                router as yandex_oauth_router,
            )

            app.include_router(identity_router)
            app.include_router(yandex_oauth_router)
            if include_legacy_router:
                from app.api.legacy_router import router as legacy_router

                app.include_router(legacy_router)

        install_error_handlers(app)
        return app
    except Exception:
        closer.close()
        raise
