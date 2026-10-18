"""Unit analysis engine.

Reads raw test exports (one row per test execution), validates them, pivots each unit's tests
by sequence and classifies every unit (visual_id) of an operation as Recovery, True Reject or
HW-Induced Reject, following review_material.md. Every count, percentage and ranking is computed
with Polars and returned as facts in a UnitAnalysis: no narrative and no interpretation.
"""

import logging
import re
import time
from dataclasses import dataclass

import polars as pl

from app.core.exceptions import ProcessingError
from app.schemas.files import OutputFormat, UploadedFile
from app.schemas.unit import Classification, OperationMetrics, Rule, TransitionCount, UnitAnalysis
from app.services.file_io import EXTRA_FIELDS_COLUMN, read_text_table, write_dataframe

logger = logging.getLogger(__name__)

PASS_BIN = "1000000"
SDE_PREFIX = "809"
# Rows with this data_bin are discarded before anything else is computed.
DISCARDED_BIN = "5000000"
TYPICAL_MAX_SEQUENCE = 4
ARROW = " → "

# Accepted names of each required column, compared ignoring case, spaces and punctuation.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "visual_id": ("visual_id",),
    "operation": ("operation",),
    "sequence": ("sequence", "within_session_sequence_number", "sequence_number", "seq"),
    "data_bin": ("data_bin",),
    "device_end_date_time": ("device_end_date_time",),
}
# Accepted device_end_date_time formats. Spaces match any run of whitespace.
DATETIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",  # 10/13/2026 12:29:37 AM, as Excel displays it
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S%.f",  # Excel datetime cells, as read from .xlsx/.xls
    "%Y-%m-%dT%H:%M:%S%.f",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)
UNIT_KEY = ["operation", "visual_id"]
TEST_KEY = ["operation", "visual_id", "sequence"]
ROW_SCHEMA = {
    "row": pl.Int64,
    "operation": pl.String,
    "visual_id": pl.String,
    "sequence": pl.Int64,
    "data_bin": pl.String,
}
# "8748.0" (a number saved as a decimal) -> "8748"
INTEGRAL_DECIMAL = r"^([+-]?\d+)\.0*$"
# Control characters, and U+FFFD which replaces undecodable bytes.
INVALID_CHARACTERS = r"[\x00-\x1f\x7f\x{fffd}]"
CLASSIFICATION_BY_RULE = {
    Rule.RECOVERY: Classification.RECOVERY,
    Rule.SDE_FIRST_TOUCH: Classification.TRUE_REJECT,
    Rule.SDE_LATER_SEQUENCE: Classification.HW_INDUCED,
    Rule.NO_RETEST: Classification.TRUE_REJECT,
    Rule.REPEATABLE_FAILURE: Classification.TRUE_REJECT,
    Rule.SIGNATURE_CHANGED: Classification.HW_INDUCED,
}


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    check: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


@dataclass(frozen=True, slots=True)
class UnitData:
    """Validated input of an analysis, shared by the analysis and the pivot endpoints."""

    checks: list[ValidationCheck]
    operations: list[str]
    """Selected operations, by volume."""
    rows: pl.DataFrame
    """Valid tests of the selected operations (ROW_SCHEMA)."""
    units: pl.DataFrame
    """One classified row per unit (operation, visual_id) of the selected operations."""


def parse_operations(values: list[str] | None) -> list[str]:
    """`["8748,7682", " 1234 "]` -> `["8748", "7682", "1234"]`."""
    operations: list[str] = []
    for value in values or []:
        for part in value.split(","):
            operation = re.sub(INTEGRAL_DECIMAL, r"\1", part.strip())
            if operation and operation not in operations:
                operations.append(operation)
    return operations


def analyze_unit_file(
    upload: UploadedFile, operations: list[str] | None, top_n: int
) -> UnitAnalysis:
    """Run the whole analysis on an upload. Blocking: run it in a worker thread."""
    started = time.perf_counter()
    try:
        data = load_unit_data(upload, operations)
        units_by_operation = data.units.partition_by("operation", as_dict=True)
        results = [
            _operation_metrics(operation, units_by_operation[(operation,)], top_n)
            for operation in data.operations
        ]
    except pl.exceptions.PolarsError as exc:
        raise ProcessingError(f"Could not analyze the file: {exc}") from exc

    issues = [str(check) for check in data.checks if not check.passed]
    for metrics, failed in results:
        issues += [f"Operation {metrics.operation} final validation, {check}" for check in failed]
    logger.info(
        "Unit analysis of %s: operations [%s], %d data issues in %.2fs",
        upload.filename,
        ",".join(data.operations),
        len(issues),
        time.perf_counter() - started,
    )
    return UnitAnalysis(
        filename=upload.filename,
        data_issues=issues,
        operations=[metrics for metrics, _ in results],
    )


def build_unit_pivot(
    upload: UploadedFile, operations: list[str] | None, output_format: OutputFormat
) -> bytes:
    """Drill-down: the pivot of the selected operations with each unit's classification.

    Blocking: run it in a worker thread.
    """
    try:
        data = load_unit_data(upload, operations)
        if data.rows.is_empty():
            failed = "; ".join(str(check) for check in data.checks if not check.passed)
            raise ProcessingError(f"No valid rows to pivot. {failed}")
        volume_rank = pl.DataFrame({"operation": data.operations}).with_row_index("volume_rank")
        pivot = (
            build_pivot(data.rows)
            .join(
                data.units.select(*UNIT_KEY, "classification", "rule", "path"),
                on=UNIT_KEY,
                how="left",
            )
            .join(volume_rank, on="operation", how="left")
            .sort("volume_rank", "visual_id")
            .drop("volume_rank")
        )
    except pl.exceptions.PolarsError as exc:
        raise ProcessingError(f"Could not build the pivot: {exc}") from exc
    return write_dataframe(pivot, output_format)


def load_unit_data(upload: UploadedFile, operations: list[str] | None) -> UnitData:
    """Phases 1-4: validate the file, discard and de-duplicate, select operations, classify."""
    requested = parse_operations(operations)
    table = read_text_table(upload.content, upload.file_type)
    columns = [name for name in table.columns if name != EXTRA_FIELDS_COLUMN]
    mapping = _map_columns(columns)
    empty_row = (
        pl.all_horizontal(pl.col(columns).str.strip_chars().fill_null("") == "")
        & pl.col(EXTRA_FIELDS_COLUMN).is_null()
    )
    size = table.select(rows=(~empty_row).sum(), empty_rows=empty_row.sum()).row(0, named=True)

    checks = [_file_check(size, len(columns)), _required_columns_check(columns, mapping)]
    if not all(check.passed for check in checks):
        no_rows = pl.DataFrame(schema=ROW_SCHEMA)
        return UnitData(checks=checks, operations=[], rows=no_rows, units=_classify_units(no_rows))

    tests = _parse_tests(table, mapping, empty_row)
    kept = tests.filter(pl.col("valid") & (pl.col("data_bin") != DISCARDED_BIN))
    clean = _latest_tests(kept)
    checks += _row_checks(tests, kept)

    volumes = (
        clean.group_by("operation")
        .agg(units=pl.col("visual_id").n_unique(), rows=pl.len())
        .sort(["units", "rows", "operation"], descending=[True, True, False])
    )
    if requested:
        checks.append(_requested_operations_check(requested, volumes))
        volumes = volumes.filter(pl.col("operation").is_in(requested))
    selected = volumes["operation"].to_list()
    rows = clean.filter(pl.col("operation").is_in(selected))
    return UnitData(checks=checks, operations=selected, rows=rows, units=_classify_units(rows))


def _latest_tests(tests: pl.DataFrame) -> pl.DataFrame:
    """One row per operation + visual_id + sequence: the latest by device_end_date_time.

    Rows tied for the latest time keep the first of them in the file.
    """
    flagged = tests.with_columns(duplicated=pl.struct(TEST_KEY).is_duplicated())
    latest = (
        flagged.filter("duplicated")
        .sort(["device_end", "row"], descending=[True, False])
        .unique(subset=TEST_KEY, keep="first", maintain_order=True)
    )
    return pl.concat([flagged.filter(~pl.col("duplicated")), latest]).select(list(ROW_SCHEMA))


def build_pivot(rows: pl.DataFrame) -> pl.DataFrame:
    """Phase 3: one row per unit, one SeqN column of data_bin per sequence, in numeric order."""
    sequences = rows.get_column("sequence").unique().sort()
    if sequences.is_empty():
        return pl.DataFrame(schema={"operation": pl.String, "visual_id": pl.String})
    return (
        rows.pivot(on="sequence", on_columns=sequences, index=UNIT_KEY, values="data_bin")
        .rename({str(sequence): f"Seq{sequence}" for sequence in sequences})
        .sort(UNIT_KEY)
    )


# --- Phase 1: validation ---


def _map_columns(columns: list[str]) -> dict[str, str]:
    """Required column -> the file's column, matched ignoring case, spaces and punctuation."""
    by_key: dict[str, str] = {}
    for name in columns:
        by_key.setdefault(_column_key(name), name)
    mapping: dict[str, str] = {}
    for required, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if _column_key(alias) in by_key:
                mapping[required] = by_key[_column_key(alias)]
                break
    return mapping


def _column_key(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", name.lower())


def _file_check(size: dict[str, int], columns: int) -> ValidationCheck:
    detail = f"{_count(size['rows'], 'row')} x {_count(columns, 'column')}"
    if size["empty_rows"]:
        detail += f"; {_count(size['empty_rows'], 'empty row')} ignored"
    return ValidationCheck(
        check="Raw file successfully loaded",
        passed=size["rows"] > 0,
        detail=detail if size["rows"] else "The file has no data rows",
    )


def _required_columns_check(columns: list[str], mapping: dict[str, str]) -> ValidationCheck:
    missing = [column for column in COLUMN_ALIASES if column not in mapping]
    if not missing:
        return ValidationCheck(
            check="Required columns exist",
            passed=True,
            detail=", ".join(f"{column} = {name}" for column, name in mapping.items()),
        )
    accepted = "; ".join(f"{column}: {', '.join(COLUMN_ALIASES[column])}" for column in missing)
    found = ", ".join(columns[:30]) + (", ..." if len(columns) > 30 else "")
    return ValidationCheck(
        check="Required columns exist",
        passed=False,
        detail=f"Missing {', '.join(missing)} (accepted names: {accepted}). "
        f"Columns in the file: {found}",
    )


def _parse_tests(table: pl.DataFrame, mapping: dict[str, str], empty_row: pl.Expr) -> pl.DataFrame:
    """One row per non-empty file row: trimmed values (blank -> null) and validation flags."""

    def value(column: str, numeric: bool = False) -> pl.Expr:
        text = pl.col(mapping[column]).str.strip_chars()
        if numeric:
            text = (
                pl.when(text.str.contains(".", literal=True))
                .then(text.str.replace(INTEGRAL_DECIMAL, "${1}"))
                .otherwise(text)
            )
        return pl.when(text != "").then(text)

    number = pl.col("sequence_text").cast(pl.Float64, strict=False)
    sequence_number = pl.col("sequence_number")
    return (
        table.lazy()
        .select(
            row=pl.int_range(pl.len()),
            empty=empty_row,
            operation=value("operation", numeric=True),
            visual_id=value("visual_id"),
            sequence_text=value("sequence", numeric=True),
            data_bin=value("data_bin", numeric=True),
            device_end_date_time=value("device_end_date_time"),
            extra_fields=pl.col(EXTRA_FIELDS_COLUMN).is_not_null(),
        )
        .filter(~pl.col("empty"))
        .with_columns(
            sequence_number=pl.when(number.is_finite()).then(number),
            device_end=pl.coalesce(
                [
                    pl.col("device_end_date_time").str.to_datetime(datetime_format, strict=False)
                    for datetime_format in DATETIME_FORMATS
                ]
            ),
        )
        .with_columns(
            sequence=pl.when(
                (sequence_number >= 1) & (sequence_number == sequence_number.floor())
            ).then(sequence_number.cast(pl.Int64, strict=False)),
            invalid_characters=pl.any_horizontal(
                pl.col("operation", "visual_id", "sequence_text", "data_bin")
                .str.contains(INVALID_CHARACTERS)
                .fill_null(False)
            ),
            malformed_data_bin=pl.col("data_bin").is_not_null()
            & ~pl.col("data_bin").str.contains(r"^[0-9]+$"),
        )
        .with_columns(
            corrupted=pl.col("extra_fields")
            | pl.col("invalid_characters")
            | pl.col("malformed_data_bin")
        )
        .with_columns(
            valid=pl.all_horizontal(
                pl.col("operation", "visual_id", "sequence", "data_bin", "device_end").is_not_null()
            )
            & ~pl.col("corrupted")
        )
        .collect()
    )


def _row_checks(tests: pl.DataFrame, kept: pl.DataFrame) -> list[ValidationCheck]:
    """Checks on every row (a row failing one is excluded), then on duplicate tests."""
    sequence_text = pl.col("sequence_text")
    sequence_number = pl.col("sequence_number")
    device_end_text = pl.col("device_end_date_time")
    counts = tests.select(
        sequence_not_numeric=sequence_number.is_null().sum(),
        sequence_blank=sequence_text.is_null().sum(),
        sequence_text=(sequence_text.is_not_null() & sequence_number.is_null()).sum(),
        visual_id=pl.col("visual_id").is_null().sum(),
        operation=pl.col("operation").is_null().sum(),
        data_bin=pl.col("data_bin").is_null().sum(),
        device_end_blank=device_end_text.is_null().sum(),
        device_end_invalid=(device_end_text.is_not_null() & pl.col("device_end").is_null()).sum(),
        invalid_sequence=(sequence_number.is_not_null() & pl.col("sequence").is_null()).sum(),
        above_typical=(pl.col("sequence") > TYPICAL_MAX_SEQUENCE).sum(),
        max_sequence=pl.col("sequence").max(),
        corrupted=pl.col("corrupted").sum(),
        extra_fields=pl.col("extra_fields").sum(),
        invalid_characters=pl.col("invalid_characters").sum(),
        malformed_data_bin=pl.col("malformed_data_bin").sum(),
    ).row(0, named=True)
    duplicates = (
        kept.filter(pl.struct(TEST_KEY).is_duplicated())
        .group_by(TEST_KEY)
        .agg(
            rows=pl.len(),
            bins=pl.col("data_bin").n_unique(),
            tied=(pl.col("device_end") == pl.col("device_end").max()).sum() > 1,
        )
        .select(
            keys=pl.len(),
            conflicting=(pl.col("bins") > 1).sum(),
            tied=pl.col("tied").sum(),
            excluded=(pl.col("rows") - 1).sum(),
        )
        .row(0, named=True)
    )

    sequence_detail = (
        f"{_count(counts['sequence_not_numeric'], 'row')} excluded: "
        f"{counts['sequence_blank']} blank, {counts['sequence_text']} not a number"
        if counts["sequence_not_numeric"]
        else "Every row has a numeric sequence"
    )
    invalid_detail = (
        f"{_count(counts['invalid_sequence'], 'row')} excluded: not a whole number >= 1"
        if counts["invalid_sequence"]
        else "Every numeric sequence is a whole number >= 1"
    )
    if counts["above_typical"]:
        invalid_detail += (
            f"; {_count(counts['above_typical'], 'row')} above the typical range "
            f"1-{TYPICAL_MAX_SEQUENCE} (max {counts['max_sequence']}) kept"
        )
    duplicate_detail = "Each visual_id + sequence appears once per operation"
    if duplicates["keys"]:
        duplicate_detail = (
            f"{_count(duplicates['keys'], 'visual_id + sequence combination')} repeated "
            f"within an operation ({duplicates['conflicting']} with different data_bin "
            "values); the row with the latest device_end_date_time is kept"
        )
        if duplicates["tied"]:
            duplicate_detail += (
                f" ({duplicates['tied']} tied at the latest time: the first tied row in "
                "the file is kept)"
            )
        duplicate_detail += f", {_count(duplicates['excluded'], 'row')} excluded"
    return [
        ValidationCheck(
            check="sequence is numeric",
            passed=counts["sequence_not_numeric"] == 0,
            detail=sequence_detail,
        ),
        ValidationCheck(
            check="Sequence numbers valid",
            passed=counts["invalid_sequence"] == 0,
            detail=invalid_detail,
        ),
        _blank_check("visual_id", counts["visual_id"]),
        _blank_check("operation", counts["operation"]),
        _blank_check("data_bin", counts["data_bin"]),
        _blank_check("device_end_date_time", counts["device_end_blank"]),
        ValidationCheck(
            check="device_end_date_time is a valid date and time",
            passed=counts["device_end_invalid"] == 0,
            detail=(
                f"{_count(counts['device_end_invalid'], 'row')} excluded: not a recognized "
                "date and time, such as 10/13/2026 12:29:37 AM or 2026-10-13 00:29:37"
                if counts["device_end_invalid"]
                else "Every device_end_date_time is a valid date and time"
            ),
        ),
        ValidationCheck(
            check="No corrupted rows",
            passed=counts["corrupted"] == 0,
            detail=(
                f"{_count(counts['corrupted'], 'row')} excluded: {counts['extra_fields']} with "
                f"more fields than the header, {counts['invalid_characters']} with invalid "
                f"characters, {counts['malformed_data_bin']} with a data_bin that is not a "
                "whole number"
                if counts["corrupted"]
                else "No corrupted rows"
            ),
        ),
        ValidationCheck(
            check="No duplicate visual_id + sequence",
            passed=duplicates["keys"] == 0,
            detail=duplicate_detail,
        ),
    ]


def _blank_check(column: str, blank: int) -> ValidationCheck:
    return ValidationCheck(
        check=f"{column} not blank",
        passed=blank == 0,
        detail=f"{_count(blank, 'row')} excluded" if blank else f"No blank {column}",
    )


def _requested_operations_check(requested: list[str], volumes: pl.DataFrame) -> ValidationCheck:
    in_file = volumes["operation"].to_list()
    missing = [operation for operation in requested if operation not in in_file]
    listed = ", ".join(
        f"{operation} ({_count(units, 'unit')})"
        for operation, units in volumes.head(20).select("operation", "units").iter_rows()
    )
    return ValidationCheck(
        check="Requested operations present",
        passed=not missing,
        detail=(
            f"No rows to analyze for {', '.join(missing)}. Operations found: {listed or 'none'}"
            if missing
            else f"Found {', '.join(requested)}"
        ),
    )


# --- Phase 4: classification ---


def _classify_units(rows: pl.DataFrame) -> pl.DataFrame:
    """One row per unit: its bins in sequence order, the rule that matched and its class.

    Rules apply in the order of the Rule enum and the first match wins. Seq1 and Seq2 are the
    unit's first two remaining tests in sequence order (after data_bin 5000000 rows are
    discarded), so a missing sequence number doesn't leave a gap.
    """
    bins = pl.col("bins")
    is_sde = pl.element().str.starts_with(SDE_PREFIX)
    rule = (
        pl.when(pl.col("first_pass_sequence").is_not_null())
        .then(pl.lit(Rule.RECOVERY.value))
        .when(pl.col("first_bin").str.starts_with(SDE_PREFIX))
        .then(pl.lit(Rule.SDE_FIRST_TOUCH.value))
        .when(pl.col("later_sde_bins").list.len() > 0)
        .then(pl.lit(Rule.SDE_LATER_SEQUENCE.value))
        .when(pl.col("tests") == 1)
        .then(pl.lit(Rule.NO_RETEST.value))
        .when(pl.col("second_bin") == pl.col("first_bin"))
        .then(pl.lit(Rule.REPEATABLE_FAILURE.value))
        .otherwise(pl.lit(Rule.SIGNATURE_CHANGED.value))
    )
    classification_by_rule = {
        rule.value: classification.value for rule, classification in CLASSIFICATION_BY_RULE.items()
    }
    return (
        rows.lazy()
        .group_by(UNIT_KEY)
        .agg(
            bins=pl.col("data_bin").sort_by("sequence"),
            first_pass_sequence=pl.col("sequence").filter(pl.col("data_bin") == PASS_BIN).min(),
        )
        .with_columns(
            tests=bins.list.len(),
            first_bin=bins.list.first(),
            second_bin=bins.list.get(1, null_on_oob=True),
            later_sde_bins=bins.list.slice(1).list.eval(
                pl.element().filter(is_sde).unique(maintain_order=True)
            ),
            path=bins.list.join(ARROW),
        )
        .with_columns(rule=rule)
        .with_columns(
            classification=pl.col("rule").replace_strict(
                classification_by_rule, return_dtype=pl.String
            )
        )
        .sort(UNIT_KEY)
        .collect()
    )


# --- Phases 5-7, per operation ---


def _operation_metrics(
    operation: str, units: pl.DataFrame, top_n: int
) -> tuple[OperationMetrics, list[ValidationCheck]]:
    """Metrics and HW-Induced transition Pareto of one operation, and its failed final checks."""
    classification = pl.col("classification")
    counts = (
        units.select(
            total_units=pl.len(),
            recovery=(classification == Classification.RECOVERY.value).sum(),
            true_reject=(classification == Classification.TRUE_REJECT.value).sum(),
            hw_induced=(classification == Classification.HW_INDUCED.value).sum(),
        )
        .with_columns(
            yield_pct=_share("recovery", "total_units"),
            true_reject_pct=_share("true_reject", "total_units"),
            hw_induced_pct=_share("hw_induced", "total_units"),
        )
        .row(0, named=True)
    )
    # Consecutive tests of HW-Induced units whose bin changed: Seq1 -> Seq2, Seq2 -> Seq3, ...
    changes = (
        units.lazy()
        .filter(classification == Classification.HW_INDUCED.value)
        .select(
            "visual_id",
            from_bin=pl.col("bins").list.head(pl.col("tests") - 1),
            to_bin=pl.col("bins").list.tail(pl.col("tests") - 1),
        )
        .explode("from_bin", "to_bin", empty_as_null=False)
        .filter(pl.col("from_bin") != pl.col("to_bin"))
        .collect()
    )
    transitions = _with_shares(
        changes.group_by("from_bin", "to_bin")
        .agg(count=pl.len())
        .sort(["count", "from_bin", "to_bin"], descending=[True, False, False])
    )
    metrics = OperationMetrics(
        operation=operation,
        total_units=counts["total_units"],
        total_good_units=counts["recovery"],
        yield_pct=counts["yield_pct"],
        true_reject=counts["true_reject"],
        true_reject_pct=counts["true_reject_pct"],
        hw_induced=counts["hw_induced"],
        hw_induced_pct=counts["hw_induced_pct"],
        hw_induced_pareto=[TransitionCount(**row) for row in transitions.head(top_n).to_dicts()],
    )
    checks = _final_validation(units, changes, transitions, metrics)
    return metrics, [check for check in checks if not check.passed]


def _final_validation(
    units: pl.DataFrame,
    changes: pl.DataFrame,
    transitions: pl.DataFrame,
    metrics: OperationMetrics,
) -> list[ValidationCheck]:
    """Phase 7 checks, recomputed from the classified units rather than trusted."""
    classification = pl.col("classification")
    recovered = pl.col("first_pass_sequence").is_not_null()
    first_sde = pl.col("first_bin").str.starts_with(SDE_PREFIX)
    later_sde = pl.col("later_sde_bins").list.len() > 0
    # Units left to Rules 4 and 5, and how Seq1 compares with Seq2 for them.
    no_pass_or_sde = ~recovered & ~first_sde & ~later_sde
    single_or_same = (pl.col("tests") == 1) | (pl.col("second_bin") == pl.col("first_bin"))
    changed = (pl.col("tests") > 1) & (pl.col("second_bin") != pl.col("first_bin"))
    true_reject = Classification.TRUE_REJECT.value
    hw_induced = Classification.HW_INDUCED.value
    found = units.select(
        recovered_as_reject=(recovered & (classification != Classification.RECOVERY.value)).sum(),
        sde_misclassified=(
            (~recovered & first_sde & (classification != true_reject))
            | (~recovered & ~first_sde & later_sde & (classification != hw_induced))
        ).sum(),
        seq_misclassified=(
            no_pass_or_sde
            & (
                (single_or_same & (classification != true_reject))
                | (changed & (classification != hw_induced))
            )
        ).sum(),
    ).row(0, named=True)
    total = metrics.total_units
    good = metrics.total_good_units
    classified = good + metrics.true_reject + metrics.hw_induced
    hw_with_change = changes["visual_id"].n_unique()
    pareto_total = int(transitions["count"].sum())
    exact = tuple(part / total * 100 for part in (good, metrics.true_reject, metrics.hw_induced))
    reported = (metrics.yield_pct, metrics.true_reject_pct, metrics.hw_induced_pct)
    return [
        ValidationCheck(
            check="Total Good Units + True Reject + HW-Induced = Total Units",
            passed=classified == total,
            detail=f"{good} + {metrics.true_reject} + {metrics.hw_induced} = {classified}, "
            f"total units {total}",
        ),
        ValidationCheck(
            check="No passing units included in reject counts",
            passed=found["recovered_as_reject"] == 0,
            detail=f"{_count(found['recovered_as_reject'], 'unit')} with a pass bin counted as "
            "reject",
        ),
        ValidationCheck(
            check="SDE classifications",
            passed=found["sde_misclassified"] == 0,
            detail=f"{_count(found['sde_misclassified'], 'unit')} with an SDE bin misclassified",
        ),
        ValidationCheck(
            check="Seq1 = Seq2 as True Reject, Seq1 != Seq2 as HW-Induced",
            passed=found["seq_misclassified"] == 0,
            detail=f"{_count(found['seq_misclassified'], 'unit')} misclassified",
        ),
        ValidationCheck(
            check="Transition paths",
            passed=hw_with_change == metrics.hw_induced and pareto_total == changes.height,
            detail=f"{hw_with_change} of {metrics.hw_induced} HW-Induced units with a bin "
            f"change; {pareto_total} of {changes.height} transitions in the Pareto",
        ),
        ValidationCheck(
            check="Pareto sorted descending",
            passed=transitions["count"].is_sorted(descending=True),
            detail="HW-Induced transitions not sorted by count",
        ),
        ValidationCheck(
            check="Percentages",
            # Each is rounded to 2 decimals, so it is off by at most 0.005 and their sum by 0.015.
            passed=all(abs(r - e) <= 0.0051 for r, e in zip(reported, exact, strict=True))
            and abs(sum(reported) - 100) <= 0.0151,
            detail=f"reported {reported}, computed {tuple(round(e, 4) for e in exact)}",
        ),
    ]


def _with_shares(counts: pl.DataFrame) -> pl.DataFrame:
    """Add each row's share of the total count and the running share, in % (rows sorted)."""
    total = pl.col("count").sum()
    return counts.with_columns(
        pct=(pl.col("count") / total * 100).round(2),
        cumulative_pct=(pl.col("count").cum_sum() / total * 100).round(2),
    )


def _share(part: str, whole: str) -> pl.Expr:
    """`part` / `whole` in %, rounded to 2 decimals; null when `whole` is 0."""
    return pl.when(pl.col(whole) > 0).then((pl.col(part) / pl.col(whole) * 100).round(2))


def _count(count: int, noun: str) -> str:
    """'1 unit', '2 units'."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
