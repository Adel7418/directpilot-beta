from fastapi import FastAPI

from app.bootstrap.dependencies import (
    ApplicationDependencies,
    create_application_dependencies,
)
from app.core.errors import install_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.modules.identity.router import router as identity_router
from app.modules.sessions.middleware import (
    CookieCsrfMiddleware,
    SessionWorkspaceContextMiddleware,
)


def create_app(*, dependencies: ApplicationDependencies | None = None) -> FastAPI:
    """Create the DirectPilot ASGI application with stable public metadata."""
    app = FastAPI(
        title="DirectPilot Beta API",
        version="0.2.1",
        description="Standalone API-first beta app for safe Yandex Direct automation.",
    )
    app.state.dependencies = (
        dependencies if dependencies is not None else create_application_dependencies()
    )
    app.add_middleware(SessionWorkspaceContextMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CookieCsrfMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(identity_router)
    install_error_handlers(app)
    return app
