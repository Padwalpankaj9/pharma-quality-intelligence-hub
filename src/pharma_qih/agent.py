"""Governed Quality Investigation Agent with RBAC, security guardrails, and OTel telemetry."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from pharma_qih.config import DB_PATH
from pharma_qih.monitoring import record_run
from pharma_qih.security import (
    check_input_safety,
    is_mcp_tool_allowed,
    sanitize_retrieved_text,
)
from pharma_qih.store import (
    get_fair_metadata,
    get_firm_risk_summary,
    get_monthly_anomalies,
    get_root_cause_trends,
    search_recalls,
)

# Maps natural-language keywords to our standardized root-cause categories
CATEGORY_HINTS: dict[str, str] = {
    "steril": "Sterility & Contamination",
    "contaminat": "Sterility & Contamination",
    "microb": "Sterility & Contamination",
    "nitrosamine": "Impurity & Nitrosamine",
    "impurit": "Impurity & Nitrosamine",
    "poten": "Potency, Assay & Stability",
    "stability": "Potency, Assay & Stability",
    "dissolution": "Potency, Assay & Stability",
    "cgmp": "CGMP Deviations",
    "gmp": "CGMP Deviations",
    "particulate": "Foreign Matter & Particulate",
    "glass": "Foreign Matter & Particulate",
    "label": "Labeling & Packaging",
    "packag": "Labeling & Packaging",
}


def _select_tool(q_lower: str) -> str:
    """Pick the single governed Gold tool that best answers the user's question."""
    if any(k in q_lower for k in ("fair", "metadata", "lineage", "owner", "schema", "freshness", "license")):
        return "get_fair_metadata"
    if any(k in q_lower for k in ("anomal", "spike", "unusual", "surge")):
        return "get_monthly_anomalies"
    if any(k in q_lower for k in ("firm", "company", "supplier", "highest risk", "top risk")):
        return "get_firm_risk_summary"
    if any(k in q_lower for k in ("trend", "root cause", "most common", "breakdown")):
        return "get_root_cause_trends"
    return "search_recalls"


def ask_quality_agent(
    question: str,
    *,
    role: str = "quality_analyst",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Answer a Quality question with input guardrails, tool-level RBAC, and citations."""
    target_db = db_path or DB_PATH
    started = time.perf_counter()
    input_tokens = max(1, len(question) // 4)
    safety = check_input_safety(question)

    if not safety.allowed:
        latency_ms = (time.perf_counter() - started) * 1000.0
        run_id, trace_id = record_run(
            interface="agent",
            gen_ai_operation_name="invoke_agent",
            tool_name="ask_quality_agent",
            role=role,
            latency_ms=latency_ms,
            status="blocked",
            blocked_reason=safety.reason,
            tool_call_count=0,
            input_tokens=input_tokens,
            db_path=target_db,
        )
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "status": "blocked",
            "blocked_reason": safety.reason,
            "answer": "Request blocked by AI security guardrails.",
            "tools_used": [],
            "citations": [],
        }

    q_lower = question.lower()
    tool_name = _select_tool(q_lower)

    # Enforce OWASP ASI03 tool-level RBAC before touching any Gold table
    if not is_mcp_tool_allowed(role, tool_name):
        latency_ms = (time.perf_counter() - started) * 1000.0
        run_id, trace_id = record_run(
            interface="agent",
            gen_ai_operation_name="invoke_agent",
            tool_name=tool_name,
            role=role,
            latency_ms=latency_ms,
            status="blocked",
            blocked_reason="rbac_tool_denied",
            tool_call_count=0,
            input_tokens=input_tokens,
            db_path=target_db,
        )
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "status": "blocked",
            "blocked_reason": "rbac_tool_denied",
            "answer": f"Role '{role}' is not permitted to run tool '{tool_name}'.",
            "tools_used": [],
            "citations": [],
        }

    citations: list[str] = []
    lines: list[str] = []

    try:
        if tool_name == "get_fair_metadata":
            meta = get_fair_metadata(db_path=target_db)
            for item in meta:
                lines.append(
                    f"{item['table_name']} ({item['dataset_id']}): schema v{item['schema_version']}, "
                    f"owner {item['owner']}, license {item['license']}, {item['row_count']} rows, "
                    f"DQ passed={item['dq_passed']}."
                )
                citations.append(f"table:{item['table_name']}@{item['schema_version']}")

        elif tool_name == "get_monthly_anomalies":
            anomalies = get_monthly_anomalies(db_path=target_db)
            flagged = [a for a in anomalies if a["is_anomaly"]]
            if flagged:
                for item in flagged[:5]:
                    lines.append(
                        f"Month {item['report_month']} had {item['recall_count']} recalls "
                        f"(mean {item['mean_monthly_recalls']}, Z={item['z_score']})."
                    )
                    citations.append(f"month:{item['report_month']}")
            else:
                lines.append("No months exceeded the 1.5 Z-score anomaly threshold.")
                for item in anomalies[:3]:
                    lines.append(
                        f"Month {item['report_month']}: {item['recall_count']} recalls (Z={item['z_score']})."
                    )
                    citations.append(f"month:{item['report_month']}")

        elif tool_name == "get_firm_risk_summary":
            firms = get_firm_risk_summary(limit=5, db_path=target_db)
            for item in firms:
                lines.append(
                    f"{item['recalling_firm']}: risk score {item['risk_score']} "
                    f"({item['total_recalls']} recalls, {item['class_i_count']} Class I, "
                    f"top cause: {item['top_root_cause']})."
                )
                citations.append(f"firm:{item['recalling_firm']}")

        elif tool_name == "get_root_cause_trends":
            trends = get_root_cause_trends(db_path=target_db)
            for item in trends[:6]:
                lines.append(
                    f"{item['report_year']} | {item['root_cause_category']}: "
                    f"{item['recall_count']} recalls."
                )
                citations.append(f"trend:{item['report_year']}:{item['root_cause_category']}")

        else:
            classification = None
            if "class iii" in q_lower or "class 3" in q_lower:
                classification = "Class III"
            elif "class ii" in q_lower or "class 2" in q_lower:
                classification = "Class II"
            elif "class i " in f"{q_lower} " or "class 1" in q_lower:
                classification = "Class I"

            root_cause = None
            for keyword, category in CATEGORY_HINTS.items():
                if keyword in q_lower:
                    root_cause = category
                    break

            matches = search_recalls(
                classification=classification,
                root_cause_category=root_cause,
                limit=5,
                db_path=target_db,
            )
            if not matches:
                lines.append("No matching recall records found for those filters.")
            else:
                for rec in matches:
                    safe_reason = sanitize_retrieved_text(rec["reason_for_recall"])
                    lines.append(
                        f"[{rec['recall_number']}] {rec['recalling_firm']} ({rec['classification']}, "
                        f"{rec['report_date']}): {rec['root_cause_category']} - {safe_reason}"
                    )
                    citations.append(rec["recall_number"])
    except (sqlite3.Error, ValueError, KeyError) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        run_id, trace_id = record_run(
            interface="agent",
            gen_ai_operation_name="invoke_agent",
            tool_name=tool_name,
            role=role,
            latency_ms=latency_ms,
            status="error",
            error_type=type(exc).__name__,
            tool_call_count=1,
            input_tokens=input_tokens,
            db_path=target_db,
        )
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "status": "error",
            "blocked_reason": None,
            "answer": "Unable to complete Quality query due to an internal data error.",
            "tools_used": [tool_name],
            "citations": [],
        }

    answer = sanitize_retrieved_text("\n".join(lines))
    latency_ms = (time.perf_counter() - started) * 1000.0
    output_tokens = max(1, len(answer) // 4)

    run_id, trace_id = record_run(
        interface="agent",
        gen_ai_operation_name="invoke_agent",
        tool_name=tool_name,
        role=role,
        latency_ms=latency_ms,
        status="ok",
        tool_call_count=1,
        citation_count=len(citations),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        db_path=target_db,
    )

    return {
        "run_id": run_id,
        "trace_id": trace_id,
        "status": "ok",
        "blocked_reason": None,
        "answer": answer,
        "tools_used": [tool_name],
        "citations": citations,
    }
