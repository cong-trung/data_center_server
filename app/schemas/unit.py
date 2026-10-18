from enum import StrEnum

from pydantic import BaseModel, Field


class Classification(StrEnum):
    RECOVERY = "Recovery"
    TRUE_REJECT = "True Reject"
    HW_INDUCED = "HW-Induced Reject"


class Rule(StrEnum):
    """Classification rules, listed in the order they are applied (the first match wins)."""

    RECOVERY = "rule1_recovery"
    SDE_FIRST_TOUCH = "rule2_sde_first_touch"
    SDE_LATER_SEQUENCE = "rule3_sde_later_sequence"
    NO_RETEST = "no_retest"
    REPEATABLE_FAILURE = "rule4_repeatable_failure"
    SIGNATURE_CHANGED = "rule5_signature_changed"


class TransitionCount(BaseModel):
    """A bin change between two consecutive tests of HW-Induced units."""

    from_bin: str = Field(description="data_bin of a test")
    to_bin: str = Field(description="The different data_bin of the unit's next test")
    count: int = Field(description="Times this change happened; a unit changing twice counts twice")
    pct: float = Field(description="Share of the operation's HW-Induced transitions, in %")
    cumulative_pct: float = Field(
        description="Share of this and the more frequent transitions, in %"
    )


class OperationMetrics(BaseModel):
    """One operation. Percentages are rounded to 2 decimals."""

    operation: str
    total_units: int = Field(description="Units (distinct visual_id) analyzed")
    total_good_units: int = Field(
        description="Units that passed (data_bin 1000000) on any test: the Recovery units"
    )
    yield_pct: float = Field(description="Total Good Units / Total Units, in %")
    true_reject: int = Field(description="True Reject units")
    true_reject_pct: float = Field(description="True Reject / Total Units, in %")
    hw_induced: int = Field(description="HW-Induced Reject units")
    hw_induced_pct: float = Field(description="HW-Induced Reject / Total Units, in %")
    hw_induced_pareto: list[TransitionCount] = Field(
        description="Bin changes between consecutive tests (Seq1 -> Seq2, Seq2 -> Seq3, ...) of "
        "HW-Induced units, most frequent first, top N"
    )


class UnitAnalysis(BaseModel):
    filename: str
    data_issues: list[str] = Field(
        description="Problems found in the data (e.g. rows excluded and why) or a final validation "
        "check that failed. Empty when there is none"
    )
    operations: list[OperationMetrics] = Field(description="Analyzed operations, by volume")
