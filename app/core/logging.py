import logging.config


def setup_logging(level: str) -> None:
    """Route logs from `app.*` loggers to stderr. Uvicorn keeps its own logging config."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                },
            },
            "loggers": {
                "app": {"handlers": ["console"], "level": level, "propagate": False},
            },
        }
    )
