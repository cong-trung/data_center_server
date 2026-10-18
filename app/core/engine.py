import logging

import polars as pl

from app.core.config import PolarsEngine

logger = logging.getLogger(__name__)


def configure_polars_engine(engine: PolarsEngine) -> PolarsEngine:
    """Make `engine` the default for `.collect()` and return the engine actually in use.

    The GPU engine is probed first (which also initializes CUDA ahead of the first request).
    Without a usable GPU, fall back to CPU instead of failing every request.
    """
    if engine == "gpu":
        try:
            pl.LazyFrame({"x": [1]}).select(pl.col("x").sum()).collect(
                engine=pl.GPUEngine(raise_on_fail=True)
            )
        except Exception as exc:
            logger.warning("GPU engine unavailable, falling back to CPU: %s", exc)
            engine = "auto"
    pl.Config.set_engine_affinity(engine)
    return engine
