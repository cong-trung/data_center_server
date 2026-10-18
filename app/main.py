import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.main import api_router
from app.core.config import settings
from app.core.engine import configure_polars_engine
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import setup_logging
from app.core.middleware import MaxBodySizeMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.polars_engine = await anyio.to_thread.run_sync(
        configure_polars_engine, settings.POLARS_ENGINE
    )
    app.state.processing_limiter = anyio.CapacityLimiter(settings.MAX_CONCURRENT_JOBS)
    logger.info(
        "Starting %s v%s (environment=%s, polars_engine=%s, max_concurrent_jobs=%d)",
        settings.PROJECT_NAME,
        __version__,
        settings.ENVIRONMENT,
        app.state.polars_engine,
        settings.MAX_CONCURRENT_JOBS,
    )
    yield
    logger.info("Shutting down")


def create_app() -> FastAPI:
    setup_logging(settings.LOG_LEVEL)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=__version__,
        lifespan=lifespan,
        exception_handlers={
            AppError: app_error_handler,
            Exception: unhandled_error_handler,
        },
    )

    # The last middleware added is the outermost: keep CORS outside so 413s carry CORS headers.
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_body_size=settings.max_upload_size_bytes,
        max_json_body_size=settings.max_json_body_size_bytes,
    )
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Content-Disposition"],
        )

    app.include_router(api_router, prefix=settings.API_V1_STR)
    return app


app = create_app()
