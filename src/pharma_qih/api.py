"""Governed FastAPI REST service for the FAIR Pharma Quality Data Product."""

from __future__ import annotations

import sqlite3
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from pharma_qih import __version__
from pharma_qih.agent import ask_quality_agent
from pharma_qih.models import (
    AgentResponse,
    FairMetadataRecord,
    FirmRiskRecord,
    MonitoringSummary,
    MonthlyAnomalyRecord,
    RecallRecord,
    RootCauseTrendRecord,
    ToolResponseAnomalies,
    ToolResponseFirmRisk,
    ToolResponseMetadata,
    ToolResponseRecalls,
    ToolResponseTrends,
)
from pharma_qih.monitoring import get_monitoring_summary, record_run
from pharma_qih.security import (
    check_input_safety,
    is_endpoint_allowed,
    resolve_role,
    sanitize_retrieved_text,
)
from pharma_qih.store import (
    get_fair_metadata,
    get_firm_risk_summary,
    get_monthly_anomalies,
    get_root_cause_trends,
    search_recalls,
)

app = FastAPI(
    title="Pharma Quality Intelligence Hub API",
    version=__version__,
    description="Governed FAIR Pharma Quality Data Product with APIKeyHeader RBAC, MCP v2 parity, and AI guardrails.",
)

# Registers X-API-Key in OpenAPI securitySchemes so /docs shows the Authorize button
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class AgentQuestionRequest(BaseModel):
    """Request payload for the Quality investigation agent."""

    question: str = Field(..., min_length=2, max_length=1500)


class HealthResponse(BaseModel):
    """Liveness response."""

    status: str
    version: str


class MonitoringResponse(BaseModel):
    """Monitoring summary response."""

    role: str
    summary: MonitoringSummary


@app.exception_handler(sqlite3.Error)
async def sqlite_error_handler(_request: Request, _exc: sqlite3.Error) -> JSONResponse:
    """Return a clean 503 instead of a raw 500 if the Gold database is unavailable."""
    return JSONResponse(
        status_code=503,
        content={"detail": "FAIR Gold data store is temporarily unavailable."},
    )


def require_rbac(request: Request, x_api_key: str | None = Depends(api_key_header)) -> str:
    """Authenticate API key, enforce RBAC, and audit 401/403 denials in telemetry."""
    role = resolve_role(x_api_key)
    if not role:
        record_run(
            interface="api",
            tool_name=f"{request.method} {request.url.path}",
            role="anonymous",
            latency_ms=0.1,
            status="blocked",
            blocked_reason="invalid_api_key",
            tool_call_count=0,
        )
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key.")
    if not is_endpoint_allowed(role, request.url.path):
        record_run(
            interface="api",
            tool_name=f"{request.method} {request.url.path}",
            role=role,
            latency_ms=0.1,
            status="blocked",
            blocked_reason="rbac_endpoint_denied",
            tool_call_count=0,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Role '{role}' is not permitted to access {request.url.path}.",
        )
    return role


def _guard_params(route_name: str, role: str, started: float, values: list[str | None]) -> None:
    """Check optional string query params for injection payloads and log blocks."""
    for val in values:
        if val:
            safety = check_input_safety(val)
            if not safety.allowed:
                record_run(
                    interface="api",
                    tool_name=route_name,
                    role=role,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    status="blocked",
                    blocked_reason=safety.reason,
                    tool_call_count=0,
                )
                raise HTTPException(status_code=400, detail=f"Blocked query: {safety.reason}")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check endpoint."""
    return HealthResponse(status="ok", version=__version__)


@app.get("/v1/metadata", response_model=ToolResponseMetadata)
def list_metadata(
    table_name: str | None = Query(default=None),
    role: str = Depends(require_rbac),
) -> ToolResponseMetadata:
    """Return FAIR metadata, license, lineage, and data-quality status."""
    started = time.perf_counter()
    _guard_params("GET /v1/metadata", role, started, [table_name])
    rows = [FairMetadataRecord(**r) for r in get_fair_metadata(table_name=table_name)]
    record_run(
        interface="api",
        tool_name="GET /v1/metadata",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseMetadata(status="ok", count=len(rows), tables=rows)


@app.get("/v1/recalls", response_model=ToolResponseRecalls)
def list_recalls(
    query: str | None = Query(default=None),
    classification: str | None = Query(default=None),
    root_cause_category: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    role: str = Depends(require_rbac),
) -> ToolResponseRecalls:
    """Search governed Gold drug recall records with pagination and sanitization."""
    started = time.perf_counter()
    _guard_params("GET /v1/recalls", role, started, [query, classification, root_cause_category])

    rows = [
        RecallRecord(
            **{
                **r,
                "reason_for_recall": sanitize_retrieved_text(r["reason_for_recall"]),
                "product_description": sanitize_retrieved_text(r["product_description"]),
            }
        )
        for r in search_recalls(
            query=query,
            classification=classification,
            root_cause_category=root_cause_category,
            limit=limit,
            offset=offset,
        )
    ]
    record_run(
        interface="api",
        tool_name="GET /v1/recalls",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseRecalls(status="ok", count=len(rows), results=rows)


@app.get("/v1/firms/risk", response_model=ToolResponseFirmRisk)
def firm_risk(
    firm_name: str | None = Query(default=None),
    min_recalls: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    role: str = Depends(require_rbac),
) -> ToolResponseFirmRisk:
    """Return firms ranked by patient-safety weighted recall risk score."""
    started = time.perf_counter()
    _guard_params("GET /v1/firms/risk", role, started, [firm_name])
    rows = [
        FirmRiskRecord(**r)
        for r in get_firm_risk_summary(
            firm_name=firm_name,
            min_recalls=min_recalls,
            limit=limit,
            offset=offset,
        )
    ]
    record_run(
        interface="api",
        tool_name="GET /v1/firms/risk",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseFirmRisk(status="ok", count=len(rows), results=rows)


@app.get("/v1/trends/root-causes", response_model=ToolResponseTrends)
def root_cause_trends(
    year_from: str | None = Query(default=None),
    year_to: str | None = Query(default=None),
    role: str = Depends(require_rbac),
) -> ToolResponseTrends:
    """Return yearly recall counts grouped by standardized Quality root cause."""
    started = time.perf_counter()
    _guard_params("GET /v1/trends/root-causes", role, started, [year_from, year_to])
    rows = [RootCauseTrendRecord(**r) for r in get_root_cause_trends(year_from=year_from, year_to=year_to)]
    record_run(
        interface="api",
        tool_name="GET /v1/trends/root-causes",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseTrends(status="ok", count=len(rows), results=rows)


@app.get("/v1/anomalies", response_model=ToolResponseAnomalies)
def monthly_anomalies(
    z_threshold: float = Query(default=1.5),
    only_anomalies: bool = Query(default=False),
    role: str = Depends(require_rbac),
) -> ToolResponseAnomalies:
    """Return monthly recall counts and Z-score anomaly flags."""
    started = time.perf_counter()
    rows = [
        MonthlyAnomalyRecord(**r)
        for r in get_monthly_anomalies(z_threshold=z_threshold, only_anomalies=only_anomalies)
    ]
    record_run(
        interface="api",
        tool_name="GET /v1/anomalies",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseAnomalies(status="ok", count=len(rows), results=rows)


@app.post("/v1/agent/ask", response_model=AgentResponse)
def agent_ask(
    body: AgentQuestionRequest,
    role: str = Depends(require_rbac),
) -> AgentResponse:
    """Ask the governed Quality investigation agent a question."""
    return AgentResponse(**ask_quality_agent(body.question, role=role))


@app.get("/v1/monitoring/summary", response_model=MonitoringResponse)
def monitoring_summary(role: str = Depends(require_rbac)) -> MonitoringResponse:
    """Return live latency, citation coverage, token usage, and blocked security metrics."""
    return MonitoringResponse(role=role, summary=MonitoringSummary(**get_monitoring_summary()))


def main() -> None:
    """Start the local Uvicorn API server."""
    uvicorn.run("pharma_qih.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
