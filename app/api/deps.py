import binascii
from pathlib import PurePath
from typing import Annotated

import anyio
from fastapi import Depends, File, Request, UploadFile

from app.core.config import settings
from app.core.exceptions import FileTooLargeError, InvalidFileError, UnsupportedFileTypeError
from app.schemas.files import FILE_TYPE_BY_EXTENSION, Base64File, FileType, UploadedFile

URL_SAFE_ALPHABET = str.maketrans("-_", "+/")


async def get_uploaded_file(
    file: Annotated[UploadFile, File(description="CSV (.csv) or Excel (.xlsx, .xls) file")],
) -> UploadedFile:
    filename = file.filename or "upload"
    file_type = _file_type(filename)
    return _uploaded_file(filename, file_type, await file.read())


def get_base64_uploaded_file(body: Base64File) -> UploadedFile:
    """The JSON-body counterpart of get_uploaded_file: same checks, file decoded in memory.

    A plain function, so FastAPI runs it (and the decoding) in a worker thread.
    """
    file_type = _file_type(body.filename)
    content = _decode_base64(body.content_base64)
    if len(content) > settings.max_upload_size_bytes:
        raise FileTooLargeError(
            f"The file exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit once decoded"
        )
    return _uploaded_file(body.filename, file_type, content)


def get_processing_limiter(request: Request) -> anyio.CapacityLimiter:
    return request.app.state.processing_limiter


def _file_type(filename: str) -> FileType:
    file_type = FILE_TYPE_BY_EXTENSION.get(PurePath(filename).suffix.lower())
    if file_type is None:
        allowed = ", ".join(FILE_TYPE_BY_EXTENSION)
        raise UnsupportedFileTypeError(f"Unsupported file type. Allowed extensions: {allowed}")
    return file_type


def _uploaded_file(filename: str, file_type: FileType, content: bytes) -> UploadedFile:
    if not content:
        raise InvalidFileError("The uploaded file is empty")
    return UploadedFile(filename=filename, file_type=file_type, content=content)


def _decode_base64(text: str) -> bytes:
    """Decode standard or URL-safe base64, with or without line breaks, padding or data URL prefix.

    Each normalization step only copies the text when it has something to change.
    """
    if text[:5].lower() == "data:":
        header, comma, text = text.partition(",")
        if not comma or not header.lower().endswith(";base64"):
            raise InvalidFileError("content_base64 is a data URL without ';base64,'")
    if any(whitespace in text for whitespace in "\n\r\t "):
        text = "".join(text.split())
    if "-" in text or "_" in text:
        text = text.translate(URL_SAFE_ALPHABET)
    text += "=" * (-len(text) % 4)
    try:
        return binascii.a2b_base64(text, strict_mode=True)
    except ValueError as exc:
        raise InvalidFileError(f"content_base64 is not valid base64: {exc}") from exc


UploadedFileDep = Annotated[UploadedFile, Depends(get_uploaded_file)]
Base64UploadedFileDep = Annotated[UploadedFile, Depends(get_base64_uploaded_file)]
ProcessingLimiterDep = Annotated[anyio.CapacityLimiter, Depends(get_processing_limiter)]
