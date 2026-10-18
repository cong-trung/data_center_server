import io

import fastexcel
import polars as pl

from app.core.exceptions import InvalidFileError
from app.schemas.files import FileType, OutputFormat

# Rows scanned to infer column types; higher than Polars' default (100) to cope with messy files.
INFER_SCHEMA_LENGTH = 10_000
# Added by read_text_table: first surplus field of CSV rows with more fields than the header.
EXTRA_FIELDS_COLUMN = "__extra_fields__"


def read_dataframe(content: bytes, file_type: FileType) -> pl.DataFrame:
    """Parse an uploaded CSV/Excel file (first sheet) into a DataFrame."""
    try:
        match file_type:
            case FileType.CSV:
                return pl.read_csv(content, infer_schema_length=INFER_SCHEMA_LENGTH)
            case FileType.EXCEL:
                return pl.read_excel(content, infer_schema_length=INFER_SCHEMA_LENGTH)
    except (pl.exceptions.PolarsError, fastexcel.FastExcelError) as exc:
        raise _parse_error(file_type, exc) from exc


def read_text_table(content: bytes, file_type: FileType) -> pl.DataFrame:
    """Parse a CSV/Excel file (first sheet) with every column as text, keeping malformed rows.

    Undecodable bytes become U+FFFD and rows with missing fields get nulls. A CSV row with more
    fields than the header keeps its first surplus field in EXTRA_FIELDS_COLUMN (null for every
    other row) instead of failing the whole file.
    """
    try:
        match file_type:
            case FileType.CSV:
                header = pl.read_csv(
                    content,
                    n_rows=0,
                    infer_schema=False,
                    truncate_ragged_lines=True,
                    encoding="utf8-lossy",
                ).columns
                schema = dict.fromkeys([*header, EXTRA_FIELDS_COLUMN], pl.String)
                try:
                    return pl.read_csv(
                        content,
                        has_header=False,
                        skip_rows=1,
                        schema=schema,
                        truncate_ragged_lines=True,
                        encoding="utf8-lossy",
                    )
                except pl.exceptions.NoDataError:
                    return pl.DataFrame(schema=schema)  # Header only
            case FileType.EXCEL:
                return pl.read_excel(content, infer_schema_length=0).with_columns(
                    pl.lit(None, pl.String).alias(EXTRA_FIELDS_COLUMN)
                )
    except (pl.exceptions.PolarsError, fastexcel.FastExcelError) as exc:
        raise _parse_error(file_type, exc) from exc


def _parse_error(file_type: FileType, exc: Exception) -> InvalidFileError:
    # The first line holds the reason; the rest is parser-internal context.
    reason = str(exc).strip().partition("\n")[0] or type(exc).__name__
    return InvalidFileError(f"Could not parse the {file_type} file: {reason}")


def write_dataframe(df: pl.DataFrame, output_format: OutputFormat) -> bytes:
    """Serialize a DataFrame into the requested output format."""
    buffer = io.BytesIO()
    match output_format:
        case OutputFormat.CSV:
            df.write_csv(buffer)
        case OutputFormat.XLSX:
            df.write_excel(buffer)
        case OutputFormat.JSON:
            df.write_json(buffer)
    return buffer.getvalue()
