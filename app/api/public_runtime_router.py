from fastapi import APIRouter

router = APIRouter()


@router.get("/health", include_in_schema=False)
def public_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
def public_ready() -> dict[str, str]:
    return {"status": "ready"}
