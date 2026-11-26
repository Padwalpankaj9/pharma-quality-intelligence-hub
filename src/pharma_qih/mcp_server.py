"""Custom Python MCP v2 server exposing FAIR Pharma Quality Gold tables and resources."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from pharma_qih.config import METADATA_DIR
from pharma_qih.models import (
    FairMetadataRecord,
    FirmRiskRecord,
    MonthlyAnomalyRecord,
    RecallRecord,
    RootCauseTrendRecord,
    ToolResponseAnomalies,
    ToolResponseFirmRisk,
    ToolResponseMetadata,
    ToolResponseRecalls,
    ToolResponseTrends,
)
from pharma_qih.monitoring import record_run
from pharma_qih.security import check_input_safety, is_mcp_tool_allowed, sanitize_retrieved_text
from pharma_qih.store import (
    get_fair_metadata,
    get_firm_risk_summary,
    get_monthly_anomalies,
    get_root_cause_trends,
    search_recalls,
)

mcp = MCPServer("pharma-quality-intelligence-hub")
READ_ONLY_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _current_mcp_role() -> str:
    """Resolve the active RBAC role for MCP sessions from environment."""
    return os.getenv("PHARMA_QIH_MCP_ROLE", "quality_analyst")


def _validate_mcp_call(tool_name: str, texts_to_check: list[str | None]) -> tuple[str, str | None]:
    """Enforce RBAC and input safety across all string parameters before running a tool."""
    role = _current_mcp_role()
    if not is_mcp_tool_allowed(role, tool_name):
        return role, "rbac_tool_denied"
    for text in texts_to_check:
        if text:
            safety = check_input_safety(text)
            if not safety.allowed:
                return role, safety.reason
    return role, None


@mcp.tool(
    name="search_recalls",
    title="Search FAIR Gold FDA Drug Recalls",
    annotations=READ_ONLY_ANNOTATIONS,
)
def mcp_search_recalls(
    query: str | None = None,
    classification: str | None = None,
    root_cause_category: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> ToolResponseRecalls:
    """Search FAIR Gold FDA drug recall records with RBAC and safety guardrails."""
    started = time.perf_counter()
    role, blocked_reason = _validate_mcp_call(
        "search_recalls",
        [query, classification, root_cause_category],
    )
    if blocked_reason:
        record_run(
            interface="mcp",
            tool_name="search_recalls",
            role=role,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="blocked",
            blocked_reason=blocked_reason,
            tool_call_count=0,
        )
        return ToolResponseRecalls(status="blocked", reason=blocked_reason)

    rows = search_recalls(
        query=query,
        classification=classification,
        root_cause_category=root_cause_category,
        limit=limit,
        offset=offset,
    )
    sanitized = [
        RecallRecord(
            **{
                **r,
                "reason_for_recall": sanitize_retrieved_text(r["reason_for_recall"]),
                "product_description": sanitize_retrieved_text(r["product_description"]),
            }
        )
        for r in rows
    ]
    record_run(
        interface="mcp",
        tool_name="search_recalls",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(sanitized),
    )
    return ToolResponseRecalls(status="ok", count=len(sanitized), results=sanitized)


@mcp.tool(
    name="get_firm_risk_summary",
    title="Get Pharma Firm Recall Risk Summary",
    annotations=READ_ONLY_ANNOTATIONS,
)
def mcp_get_firm_risk_summary(
    firm_name: str | None = None,
    min_recalls: int = 1,
    limit: int = 10,
    offset: int = 0,
) -> ToolResponseFirmRisk:
    """Return pharma firms ranked by severity-weighted recall risk score."""
    started = time.perf_counter()
    role, blocked_reason = _validate_mcp_call("get_firm_risk_summary", [firm_name])
    if blocked_reason:
        record_run(
            interface="mcp",
            tool_name="get_firm_risk_summary",
            role=role,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="blocked",
            blocked_reason=blocked_reason,
            tool_call_count=0,
        )
        return ToolResponseFirmRisk(status="blocked", reason=blocked_reason)

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
        interface="mcp",
        tool_name="get_firm_risk_summary",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseFirmRisk(status="ok", count=len(rows), results=rows)


@mcp.tool(
    name="get_root_cause_trends",
    title="Get Annual Pharma Quality Root-Cause Trends",
    annotations=READ_ONLY_ANNOTATIONS,
)
def mcp_get_root_cause_trends(
    year_from: str | None = None,
    year_to: str | None = None,
) -> ToolResponseTrends:
    """Return yearly drug recall counts grouped by Quality root-cause category."""
    started = time.perf_counter()
    role, blocked_reason = _validate_mcp_call("get_root_cause_trends", [year_from, year_to])
    if blocked_reason:
        record_run(
            interface="mcp",
            tool_name="get_root_cause_trends",
            role=role,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="blocked",
            blocked_reason=blocked_reason,
            tool_call_count=0,
        )
        return ToolResponseTrends(status="blocked", reason=blocked_reason)

    rows = [RootCauseTrendRecord(**r) for r in get_root_cause_trends(year_from=year_from, year_to=year_to)]
    record_run(
        interface="mcp",
        tool_name="get_root_cause_trends",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseTrends(status="ok", count=len(rows), results=rows)


@mcp.tool(
    name="get_monthly_anomalies",
    title="Get Monthly Recall Z-Score Anomalies",
    annotations=READ_ONLY_ANNOTATIONS,
)
def mcp_get_monthly_anomalies(
    z_threshold: float = 1.5,
    only_anomalies: bool = False,
) -> ToolResponseAnomalies:
    """Return monthly recall volumes and flag statistical Z-score spikes."""
    started = time.perf_counter()
    role, blocked_reason = _validate_mcp_call("get_monthly_anomalies", [])
    if blocked_reason:
        record_run(
            interface="mcp",
            tool_name="get_monthly_anomalies",
            role=role,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="blocked",
            blocked_reason=blocked_reason,
            tool_call_count=0,
        )
        return ToolResponseAnomalies(status="blocked", reason=blocked_reason)

    rows = [
        MonthlyAnomalyRecord(**r)
        for r in get_monthly_anomalies(z_threshold=z_threshold, only_anomalies=only_anomalies)
    ]
    record_run(
        interface="mcp",
        tool_name="get_monthly_anomalies",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseAnomalies(status="ok", count=len(rows), results=rows)


@mcp.tool(
    name="get_fair_metadata",
    title="Get FAIR Data Product Metadata & Lineage",
    annotations=READ_ONLY_ANNOTATIONS,
)
def mcp_get_fair_metadata(table_name: str | None = None) -> ToolResponseMetadata:
    """Return FAIR schema version, dataset PID, license, owner, SHA-256 lineage, and DQ status."""
    started = time.perf_counter()
    role, blocked_reason = _validate_mcp_call("get_fair_metadata", [table_name])
    if blocked_reason:
        record_run(
            interface="mcp",
            tool_name="get_fair_metadata",
            role=role,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="blocked",
            blocked_reason=blocked_reason,
            tool_call_count=0,
        )
        return ToolResponseMetadata(status="blocked", reason=blocked_reason)

    rows = [FairMetadataRecord(**r) for r in get_fair_metadata(table_name=table_name)]
    record_run(
        interface="mcp",
        tool_name="get_fair_metadata",
        role=role,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="ok",
        citation_count=len(rows),
    )
    return ToolResponseMetadata(status="ok", count=len(rows), tables=rows)


@mcp.resource("fair://metadata", name="fair_metadata", title="FAIR Gold Table Metadata")
def resource_fair_metadata() -> str:
    """Expose live FAIR metadata and SHA-256 lineage as an MCP resource."""
    payload: dict[str, Any] = {"tables": get_fair_metadata()}
    return json.dumps(payload, indent=2)


@mcp.resource("fair://policy", name="fair_access_policy", title="FAIR RBAC Access Policy")
def resource_fair_policy() -> str:
    """Expose the governed RBAC policy YAML as a read-only MCP resource."""
    return (METADATA_DIR / "access_policy.yaml").read_text(encoding="utf-8")


def main() -> None:
    """Run the MCP v2 server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
