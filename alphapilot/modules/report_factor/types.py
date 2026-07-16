"""JSON-only contracts for research-report factor extraction.

These models deliberately contain no research/backtest implementation types.  In
particular, an extracted factor is a reviewable draft rather than a FactorTask or
Qlib experiment.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportFactorModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ReportClassification(ReportFactorModel):
    relevant: bool | None = None
    label: str = "unknown"
    reason: str = ""


class ReportMetadata(ReportFactorModel):
    file_name: str
    sha256: str
    page_count: int
    parser: str
    ocr_used: bool = False
    classification: ReportClassification = Field(default_factory=ReportClassification)


class FactorViability(ReportFactorModel):
    status: Literal["viable", "unviable", "unknown"] = "unknown"
    reason: str = ""


class FactorValidation(ReportFactorModel):
    acceptable: bool = False
    code: str = "empty_expression"
    message: str = "Expression is empty."
    details: dict[str, Any] | None = None


class FactorDraft(ReportFactorModel):
    draft_id: str
    factor_name: str
    description: str = ""
    formulation: str = ""
    variables: dict[str, str] = Field(default_factory=dict)
    factor_expression: str | None = None
    source_pages: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    viability: FactorViability = Field(default_factory=FactorViability)
    validation: FactorValidation = Field(default_factory=FactorValidation)
    warnings: list[str] = Field(default_factory=list)


class ReportFactorExtractionResult(ReportFactorModel):
    schema_version: str = "1.0"
    report: ReportMetadata
    summary: str = ""
    factors: list[FactorDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CommitFactorInput(ReportFactorModel):
    draft_id: str
    factor_name: str
    factor_expression: str
    categories: list[str] = Field(default_factory=list)
