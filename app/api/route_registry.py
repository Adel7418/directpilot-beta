from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, get_type_hints

from fastapi import APIRouter

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    domain: str
    method: str
    path: str
    endpoint: Callable[..., Any]
    options: dict[str, Any]


class DomainRouteDeclarations:
    def __init__(self, registry: RouteDeclarationRegistry, domain: str) -> None:
        self._registry = registry
        self._domain = domain

    def get(self, path: str, **options: Any) -> Callable[[Endpoint], Endpoint]:
        return self._route("GET", path, options)

    def post(self, path: str, **options: Any) -> Callable[[Endpoint], Endpoint]:
        return self._route("POST", path, options)

    def patch(self, path: str, **options: Any) -> Callable[[Endpoint], Endpoint]:
        return self._route("PATCH", path, options)

    def delete(self, path: str, **options: Any) -> Callable[[Endpoint], Endpoint]:
        return self._route("DELETE", path, options)

    def _route(
        self, method: str, path: str, options: dict[str, Any]
    ) -> Callable[[Endpoint], Endpoint]:
        def register(endpoint: Endpoint) -> Endpoint:
            self._registry.add(
                RouteDeclaration(
                    domain=self._domain,
                    method=method,
                    path=path,
                    endpoint=endpoint,
                    options=options,
                )
            )
            return endpoint

        return register


class RouteDeclarationRegistry:
    def __init__(self) -> None:
        self._declarations: list[RouteDeclaration] = []

    def for_domain(self, domain: str) -> DomainRouteDeclarations:
        return DomainRouteDeclarations(self, domain)

    def add(self, declaration: RouteDeclaration) -> None:
        self._declarations.append(declaration)

    def for_domain_routes(self, domain: str) -> tuple[RouteDeclaration, ...]:
        return tuple(
            declaration
            for declaration in self._declarations
            if declaration.domain == domain
        )


def _domain_endpoint(
    handler: Callable[..., Any], module_name: str
) -> Callable[..., Any]:
    def endpoint(*args: Any, **kwargs: Any) -> Any:
        return handler(*args, **kwargs)

    signature = inspect.signature(handler)
    type_hints = get_type_hints(handler)
    setattr(
        endpoint,
        "__signature__",
        signature.replace(
            parameters=[
                parameter.replace(
                    annotation=type_hints.get(parameter.name, parameter.annotation)
                )
                for parameter in signature.parameters.values()
            ],
            return_annotation=type_hints.get("return", signature.return_annotation),
        ),
    )
    endpoint.__name__ = handler.__name__
    endpoint.__qualname__ = handler.__qualname__
    endpoint.__doc__ = handler.__doc__
    endpoint.__module__ = module_name
    return endpoint


def register_domain_routes(
    router: APIRouter,
    declarations: RouteDeclarationRegistry,
    domain: str,
    module_name: str,
) -> None:
    for declaration in declarations.for_domain_routes(domain):
        router.add_api_route(
            declaration.path,
            _domain_endpoint(declaration.endpoint, module_name),
            methods=[declaration.method],
            **declaration.options,
        )
