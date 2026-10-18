from pathlib import PurePath
from typing import Annotated

import anyio
from fastapi import APIRouter, Query, Response

from app.api.deps import ProcessingLimiterDep, UploadedFileDep
from app.api.downloads import file_download
from app.schemas.files import FileSummary, OutputFormat
from app.services.processing import process_file, summarize_file

router = APIRouter(prefix="/files", tags=["files"])


@router.post(
    "/process",
    response_class=Response,
    responses={
        200: {
            "description": "The processed file, as a download",
            "content": {output_format.media_type: {} for output_format in OutputFormat},
        }
    },
)
async def process_upload(
    upload: UploadedFileDep,
    limiter: ProcessingLimiterDep,
    output_format: Annotated[
        OutputFormat, Query(description="Format of the returned file")
    ] = OutputFormat.CSV,
) -> Response:
    """Upload a CSV/Excel file, process it with Polars and download the result."""
    content = await anyio.to_thread.run_sync(
        process_file, upload, output_format, limiter=limiter
    )
    filename = f"{PurePath(upload.filename).stem}_processed.{output_format}"
    return file_download(content, filename, output_format.media_type)


@router.post("/inspect")
async def inspect_upload(
    upload: UploadedFileDep,
    limiter: ProcessingLimiterDep,
    preview_rows: Annotated[int, Query(ge=0, le=100)] = 10,
) -> FileSummary:
    """Upload a CSV/Excel file and get its shape, column types, null counts and first rows."""
    return await anyio.to_thread.run_sync(
        summarize_file, upload, preview_rows, limiter=limiter
    )
