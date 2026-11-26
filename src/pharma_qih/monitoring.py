"""OpenTelemetry GenAI-aligned telemetry and monitoring store."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pharma_qih.config import DB_PATH


def ensure_telemetry_table(db_path: Path | None = None) -> None:
    """Create the OTel-aligned telemetry table if it does not exist yet."""
    target = db_path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_runs (
                run_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                interface TEXT NOT NULL,
                gen_ai_operation_name TEXT NOT NULL,
                gen_ai_agent_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                role TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                status TEXT NOT NULL,
                blocked_reason TEXT,
                error_type TEXT,
                tool_call_count INTEGER NOT NULL DEFAULT 0,
                citation_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def record_run(
    *,
    interface: str,
    tool_name: str,
    role: str,
    latency_ms: float,
    status: str,
    gen_ai_operation_name: str = "execute_tool",
    gen_ai_agent_name: str = "pharma_quality_agent",
    blocked_reason: str | None = None,
    error_type: str | None = None,
    tool_call_count: int = 1,
    citation_count: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    trace_id: str | None = None,
    db_path: Path | None = None,
) -> tuple[str, str]:
    """Write one OTel-aligned execution event and return (run_id, trace_id)."""
    target = db_path or DB_PATH
    ensure_telemetry_table(target)
    run_id = str(uuid.uuid4())
    active_trace = trace_id or uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            INSERT INTO telemetry_runs (
                run_id, trace_id, timestamp_utc, interface,
                gen_ai_operation_name, gen_ai_agent_name, tool_name, role,
                latency_ms, status, blocked_reason, error_type,
                tool_call_count, citation_count, input_tokens, output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                active_trace,
                now,
                interface,
                gen_ai_operation_name,
                gen_ai_agent_name,
                tool_name,
                role,
                round(latency_ms, 2),
                status,
                blocked_reason,
                error_type,
                tool_call_count,
                citation_count,
                input_tokens,
                output_tokens,
            ),
        )
        conn.commit()
    return run_id, active_trace


def get_monitoring_summary(db_path: Path | None = None) -> dict[str, Any]:
    """Summarize total runs, errors, blocked attacks, p95 latency, and token usage."""
    target = db_path or DB_PATH
    ensure_telemetry_table(target)
    with sqlite3.connect(target) as conn:
        rows = conn.execute(
            """
            SELECT status, latency_ms, citation_count, input_tokens, output_tokens
            FROM telemetry_runs
            """
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {
            "total_runs": 0,
            "success_runs": 0,
            "blocked_runs": 0,
            "error_runs": 0,
            "avg_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "avg_citations": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }

    latencies = sorted(float(r[1]) for r in rows)
    p95_idx = min(total - 1, round(0.95 * (total - 1)))
    success = sum(1 for r in rows if r[0] == "ok")
    blocked = sum(1 for r in rows if r[0] == "blocked")
    errors = sum(1 for r in rows if r[0] == "error")
    avg_citations = sum(int(r[2]) for r in rows) / total
    total_in = sum(int(r[3]) for r in rows)
    total_out = sum(int(r[4]) for r in rows)

    return {
        "total_runs": total,
        "success_runs": success,
        "blocked_runs": blocked,
        "error_runs": errors,
        "avg_latency_ms": round(sum(latencies) / total, 2),
        "p95_latency_ms": round(latencies[p95_idx], 2),
        "avg_citations": round(avg_citations, 2),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
    }
