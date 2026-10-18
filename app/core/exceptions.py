from fastapi import Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for errors returned to the client as `{"detail": ...}`."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    detail: str = "Bad request"

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class UnsupportedFileTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    detail = "Unsupported file type"


class InvalidFileError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "The uploaded file could not be read"


class FileTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    detail = "The uploaded file is too large"


class ProcessingError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "The file could not be processed"


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises the exception after responding, so the server still logs the traceback.
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )
