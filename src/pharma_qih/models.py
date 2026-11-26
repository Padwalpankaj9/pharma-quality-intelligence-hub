"""Shared Pydantic v2 response models for FastAPI and MCP v2 structured outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RecallRecord(BaseModel):
    """One normalized FDA drug recall record from the FAIR Gold layer."""

    recall_number: str
    recalling_firm: str
    classification: str
    status: str
    report_date: str
    root_cause_category: str
    reason_for_recall: str
    product_description: str
    state: str
    country: str


class FirmRiskRecord(BaseModel):
    """Severity-weighted recall risk summary for one pharmaceutical firm."""

    recalling_firm: str
    total_recalls: int
    class_i_count: int
    class_ii_count: int
    class_iii_count: int
    risk_score: int
    top_root_cause: str


class RootCauseTrendRecord(BaseModel):
    """Annual recall count for one Pharma Quality root-cause category."""

    report_year: str
    root_cause_category: str
    recall_count: int


class MonthlyAnomalyRecord(BaseModel):
    """Monthly recall volume with Z-score anomaly flag."""

    report_month: str
    recall_count: int
    mean_monthly_recalls: float
    z_score: float
    is_anomaly: bool


class FairMetadataRecord(BaseModel):
    """FAIR provenance, license, schema version, and data-quality gate status."""

    table_name: str
    dataset_id: str
    schema_version: str
    owner: str
    license: str
    source_url: str
    source_sha256: str
    row_count: int
    dq_passed: bool
    updated_at_utc: str


class ToolResponseRecalls(BaseModel):
    """Structured MCP/REST response for recall search."""

    status: str = "ok"
    reason: str | None = None
    count: int = 0
    results: list[RecallRecord] = Field(default_factory=list)


class ToolResponseFirmRisk(BaseModel):
    """Structured MCP/REST response for firm risk rankings."""

    status: str = "ok"
    reason: str | None = None
    count: int = 0
    results: list[FirmRiskRecord] = Field(default_factory=list)


class ToolResponseTrends(BaseModel):
    """Structured MCP/REST response for root-cause trends."""

    status: str = "ok"
    reason: str | None = None
    count: int = 0
    results: list[RootCauseTrendRecord] = Field(default_factory=list)


class ToolResponseAnomalies(BaseModel):
    """Structured MCP/REST response for monthly Z-score anomalies."""

    status: str = "ok"
    reason: str | None = None
    count: int = 0
    results: list[MonthlyAnomalyRecord] = Field(default_factory=list)


class ToolResponseMetadata(BaseModel):
    """Structured MCP/REST response for FAIR metadata."""

    status: str = "ok"
    reason: str | None = None
    count: int = 0
    tables: list[FairMetadataRecord] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """Structured response from the governed Quality investigation agent."""

    run_id: str
    trace_id: str
    status: str
    blocked_reason: str | None = None
    answer: str
    tools_used: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class MonitoringSummary(BaseModel):
    """Aggregated OpenTelemetry-aligned monitoring metrics."""

    total_runs: int
    success_runs: int
    blocked_runs: int
    error_runs: int
    avg_latency_ms: float
    p95_latency_ms: float
    avg_citations: float
    total_input_tokens: int
    total_output_tokens: int
