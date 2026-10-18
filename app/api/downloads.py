from urllib.parse import quote

from fastapi import Response


def file_download(content: bytes, filename: str, media_type: str) -> Response:
    """Response that clients save as `filename`."""
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


def _content_disposition(filename: str) -> str:
    # Non-ASCII names (e.g. "báo cáo.xlsx") must use the RFC 5987 `filename*` form.
    quoted = quote(filename)
    if quoted == filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=utf-8''{quoted}"
