from fastapi import APIRouter, Request

from app import __version__
from app.core.config import settings
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> HealthResponse:
    return HealthResponse(
        version=__version__,
        environment=settings.ENVIRONMENT,
        polars_engine=request.app.state.polars_engine,
    )
