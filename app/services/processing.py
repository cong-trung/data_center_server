import logging
import time

import polars as pl

from app.core.exceptions import ProcessingError
from app.schemas.files import FileSummary, OutputFormat, UploadedFile
from app.services.file_io import read_dataframe, write_dataframe

logger = logging.getLogger(__name__)


def process_dataframe(df: pl.DataFrame) -> pl.DataFrame:
    """Business logic applied to every uploaded file. Replace the example steps with your own.

    Build the pipeline with the lazy API (`df.lazy()` ... `.collect()`) so the configured
    Polars engine (`POLARS_ENGINE`, e.g. the GPU engine) can optimize and execute it.
    """
    return (
        df.lazy()
        .with_columns(pl.col(pl.String).str.strip_chars())
        .filter(~pl.all_horizontal(pl.all().is_null()))
        .collect()
    )


def process_file(upload: UploadedFile, output_format: OutputFormat) -> bytes:
    """Read, process and serialize an upload. Blocking: run it in a worker thread."""
    started = time.perf_counter()
    df = read_dataframe(upload.content, upload.file_type)
    try:
        result = process_dataframe(df)
    except pl.exceptions.PolarsError as exc:
        raise ProcessingError(f"Could not process the file: {exc}") from exc
    content = write_dataframe(result, output_format)
    logger.info(
        "Processed %s: %d rows in, %d rows out, %s output in %.2fs",
        upload.filename,
        df.height,
        result.height,
        output_format,
        time.perf_counter() - started,
    )
    return content


def summarize_file(upload: UploadedFile, preview_rows: int) -> FileSummary:
    """Describe an upload without processing it. Blocking: run it in a worker thread."""
    df = read_dataframe(upload.content, upload.file_type)
    return FileSummary(
        filename=upload.filename,
        rows=df.height,
        columns=df.width,
        column_types={name: str(dtype) for name, dtype in df.schema.items()},
        null_counts=df.null_count().row(0, named=True),
        preview=df.head(preview_rows).to_dicts(),
    )
