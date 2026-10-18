import base64
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FileType(StrEnum):
    CSV = "csv"
    EXCEL = "excel"


FILE_TYPE_BY_EXTENSION: dict[str, FileType] = {
    ".csv": FileType.CSV,
    ".xlsx": FileType.EXCEL,
    ".xls": FileType.EXCEL,
}


class OutputFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"

    @property
    def media_type(self) -> str:
        return _MEDIA_TYPES[self]


_MEDIA_TYPES: dict[OutputFormat, str] = {
    OutputFormat.CSV: "text/csv",
    OutputFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    OutputFormat.JSON: "application/json",
}


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """A validated upload, already read into memory."""

    filename: str
    file_type: FileType
    content: bytes = field(repr=False)


_EXAMPLE_CSV = (
    "visual_id,operation,within_session_sequence_number,data_bin,device_end_date_time\n"
    "U1,8748,1,98500000,10/13/2026 12:29:37 AM\n"
    "U1,8748,2,1000000,10/13/2026 1:05:12 AM\n"
    "U2,8748,1,80963315,10/13/2026 12:31:02 AM\n"
)


class Base64File(BaseModel):
    """A file sent in a JSON body, for clients that can't send multipart/form-data."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "filename": "units.csv",
                    "content_base64": base64.b64encode(_EXAMPLE_CSV.encode()).decode(),
                }
            ]
        }
    )

    filename: str = Field(
        min_length=1,
        max_length=255,
        description="File name. Its extension sets the format: .csv, .xlsx or .xls (first sheet)",
    )
    content_base64: str = Field(
        description="The file's bytes, base64-encoded. Standard or URL-safe alphabet; line "
        "breaks, missing padding and a `data:...;base64,` prefix are accepted"
    )


class FileSummary(BaseModel):
    filename: str
    rows: int
    columns: int
    column_types: dict[str, str] = Field(description="Polars data type of each column")
    null_counts: dict[str, int]
    preview: list[dict[str, Any]] = Field(description="First rows of the file")
