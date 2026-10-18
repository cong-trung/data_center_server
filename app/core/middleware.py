from starlette import status
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class MaxBodySizeMiddleware:
    """Reject requests whose body is larger than the limit with 413.

    JSON bodies may be up to `max_json_body_size` (they carry files base64-encoded), other
    bodies up to `max_body_size`. A too-large `Content-Length` is rejected before the body is
    read; bodies without one (chunked) are cut off as soon as they cross the limit.
    """

    def __init__(
        self, app: ASGIApp, max_body_size: int, max_json_body_size: int | None = None
    ) -> None:
        self.app = app
        json_size = max_json_body_size or max_body_size
        self.limits = {
            False: (max_body_size, _detail(max_body_size)),
            True: (json_size, _detail(json_size) + " for JSON requests"),
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        max_body_size, detail = self.limits[_is_json(headers.get("content-type"))]
        content_length = headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > max_body_size:
            response = JSONResponse(
                {"detail": detail}, status_code=status.HTTP_413_CONTENT_TOO_LARGE
            )
            await response(scope, receive, send)
            return

        received = 0

        async def receive_with_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body_size:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=detail)
            return message

        await self.app(scope, receive_with_limit, send)


def _is_json(content_type: str | None) -> bool:
    """Whether FastAPI parses this body as JSON (application/json or application/*+json)."""
    media_type = (content_type or "").partition(";")[0].strip().lower()
    maintype, _, subtype = media_type.partition("/")
    return maintype == "application" and (subtype == "json" or subtype.endswith("+json"))


def _detail(max_body_size: int) -> str:
    return f"Request body exceeds the {round(max_body_size / 1024 / 1024, 1):g} MB limit"
