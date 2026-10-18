from fastapi import APIRouter

from app.api.routes import files, health, unit

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(files.router)
api_router.include_router(unit.router)
