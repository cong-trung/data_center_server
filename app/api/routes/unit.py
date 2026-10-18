from pathlib import PurePath
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Query, Response

from app.api.deps import Base64UploadedFileDep, ProcessingLimiterDep, UploadedFileDep
from app.api.downloads import file_download
from app.core.config import settings
from app.schemas.files import OutputFormat, UploadedFile
from app.schemas.unit import UnitAnalysis
from app.services.unit_analysis import analyze_unit_file, build_unit_pivot

router = APIRouter(prefix="/unit", tags=["unit"])

OperationQuery = Annotated[
    list[str] | None,
    Query(
        description="Operation(s) to analyze, e.g. 8748. Repeat the parameter or separate "
        "with commas for several. Omit to analyze every operation."
    ),
]
TopNQuery = Annotated[int, Query(ge=1, le=100, description="Rows in each Pareto table")]
OutputFormatQuery = Annotated[OutputFormat, Query(description="Format of the returned file")]

PIVOT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The pivot with each unit's classification, as a download",
        "content": {output_format.media_type: {} for output_format in OutputFormat},
    }
}
JSON_BODY_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {
        "description": f"The file is larger than {settings.MAX_UPLOAD_SIZE_MB} MB once decoded, "
        "or the request body is larger than "
        f"{round(settings.max_json_body_size_bytes / 1024 / 1024, 1):g} MB"
    },
    415: {"description": "`filename` doesn't end in .csv, .xlsx or .xls"},
}


def _json_body_description(multipart_path: str, result: str) -> str:
    multipart = f"`POST {settings.API_V1_STR}/unit/{multipart_path}`"
    return f"""
Same as {multipart}, with the file sent base64-encoded in a JSON body instead of
multipart/form-data. **Intended for AI agents and workflow tools such as n8n**, which build
JSON requests reliably but not multipart file uploads.

Send `Content-Type: application/json` and:

```json
{{"filename": "units.xlsx", "content_base64": "<the file's bytes, base64-encoded>"}}
```

- The extension of `filename` sets how the file is read: .csv, or .xlsx/.xls (first sheet).
- The file is decoded in memory and never written to disk. It can be up to
  {settings.MAX_UPLOAD_SIZE_MB} MB once decoded.
- Query parameters, checks and errors are the same as for {multipart}, and the same file
  gives the same {result}. Invalid base64 returns `422`.
"""


@router.post("/analyze")
async def analyze_unit(
    upload: UploadedFileDep,
    limiter: ProcessingLimiterDep,
    operation: OperationQuery = None,
    top_n: TopNQuery = 10,
) -> UnitAnalysis:
    """Analyze the test results of every unit in a CSV/Excel file (one row per test execution).

    Validates the rows, pivots each unit's tests by sequence and classifies every unit as
    Recovery, True Reject or HW-Induced Reject. Returns, for each operation, Total Units,
    Yield, True Reject and HW-Induced (counts and % of Total Units) and the HW-Induced
    transition Pareto. Problems with the data (missing columns, invalid rows, unknown
    operations) are listed in `data_issues`, not returned as HTTP errors.
    """
    return await _analyze(upload, limiter, operation, top_n)


@router.post(
    "/analyze-json",
    summary="Analyze Unit (JSON body, for AI agents)",
    description=_json_body_description("analyze", "`UnitAnalysis`"),
    responses=JSON_BODY_RESPONSES,
)
async def analyze_unit_json(
    upload: Base64UploadedFileDep,
    limiter: ProcessingLimiterDep,
    operation: OperationQuery = None,
    top_n: TopNQuery = 10,
) -> UnitAnalysis:
    return await _analyze(upload, limiter, operation, top_n)


@router.post("/pivot", response_class=Response, responses=PIVOT_RESPONSES)
async def unit_pivot(
    upload: UploadedFileDep,
    limiter: ProcessingLimiterDep,
    operation: OperationQuery = None,
    output_format: OutputFormatQuery = OutputFormat.CSV,
) -> Response:
    """Drill-down: the visual_id x sequence pivot of data_bin, with each unit's classification."""
    return await _pivot(upload, limiter, operation, output_format)


@router.post(
    "/pivot-json",
    response_class=Response,
    summary="Unit Pivot (JSON body, for AI agents)",
    description=_json_body_description("pivot", "pivot download"),
    responses={**PIVOT_RESPONSES, **JSON_BODY_RESPONSES},
)
async def unit_pivot_json(
    upload: Base64UploadedFileDep,
    limiter: ProcessingLimiterDep,
    operation: OperationQuery = None,
    output_format: OutputFormatQuery = OutputFormat.CSV,
) -> Response:
    return await _pivot(upload, limiter, operation, output_format)


async def _analyze(
    upload: UploadedFile,
    limiter: anyio.CapacityLimiter,
    operation: list[str] | None,
    top_n: int,
) -> UnitAnalysis:
    return await anyio.to_thread.run_sync(
        analyze_unit_file, upload, operation, top_n, limiter=limiter
    )


async def _pivot(
    upload: UploadedFile,
    limiter: anyio.CapacityLimiter,
    operation: list[str] | None,
    output_format: OutputFormat,
) -> Response:
    content = await anyio.to_thread.run_sync(
        build_unit_pivot, upload, operation, output_format, limiter=limiter
    )
    filename = f"{PurePath(upload.filename).stem}_unit_pivot.{output_format}"
    return file_download(content, filename, output_format.media_type)
