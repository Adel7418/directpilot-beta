from fastapi import APIRouter

from app.api.legacy_handlers import route_declarations
from app.api.route_registry import register_domain_routes

router = APIRouter()
register_domain_routes(router, route_declarations, "direct_extensions", __name__)
