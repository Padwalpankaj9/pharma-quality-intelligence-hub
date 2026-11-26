"""Read-only query layer over the FAIR Gold SQLite tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pharma_qih.config import DB_PATH


def _connect_readonly(db_path: Path | None = None) -> sqlite3.Connection:
    """Open SQLite in read-only URI mode so query tools can never modify data."""
    target = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def search_recalls(
    *,
    query: str | None = None,
    classification: str | None = None,
    root_cause_category: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Search Gold recall records with parameterized filters and pagination."""
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    clauses: list[str] = []
    params: list[Any] = []

    if query:
        like = f"%{query.strip()}%"
        clauses.append("(recalling_firm LIKE ? OR reason_for_recall LIKE ? OR product_description LIKE ?)")
        params.extend([like, like, like])
    if classification:
        clauses.append("classification = ?")
        params.append(classification.strip())
    if root_cause_category:
        clauses.append("root_cause_category = ?")
        params.append(root_cause_category.strip())

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT recall_number, recalling_firm, classification, status,
               report_date, root_cause_category, reason_for_recall,
               product_description, state, country
        FROM gold_recalls
        {where_sql}
        ORDER BY report_date DESC, recall_number ASC
        LIMIT ? OFFSET ?
    """
    params.extend([safe_limit, safe_offset])

    with _connect_readonly(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_firm_risk_summary(
    *,
    firm_name: str | None = None,
    min_recalls: int = 1,
    limit: int = 20,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return firms ranked by patient-safety weighted recall risk score."""
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)
    clauses = ["total_recalls >= ?"]
    params: list[Any] = [max(1, min_recalls)]

    if firm_name:
        clauses.append("recalling_firm LIKE ?")
        params.append(f"%{firm_name.strip()}%")

    sql = f"""
        SELECT recalling_firm, total_recalls, class_i_count, class_ii_count,
               class_iii_count, risk_score, top_root_cause
        FROM gold_firm_risk_summary
        WHERE {' AND '.join(clauses)}
        ORDER BY risk_score DESC, total_recalls DESC
        LIMIT ? OFFSET ?
    """
    params.extend([safe_limit, safe_offset])

    with _connect_readonly(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_root_cause_trends(
    *,
    year_from: str | None = None,
    year_to: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return recall counts grouped by year and standardized root cause."""
    clauses: list[str] = []
    params: list[Any] = []
    if year_from:
        clauses.append("report_year >= ?")
        params.append(year_from.strip())
    if year_to:
        clauses.append("report_year <= ?")
        params.append(year_to.strip())

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT report_year, root_cause_category, recall_count
        FROM gold_root_cause_trends
        {where_sql}
        ORDER BY report_year DESC, recall_count DESC
    """
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_monthly_anomalies(
    *,
    z_threshold: float = 1.5,
    only_anomalies: bool = False,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return monthly recall volumes and flag spikes above the Z-score threshold."""
    sql = """
        SELECT report_month, recall_count, mean_monthly_recalls, z_score
        FROM gold_monthly_anomalies
        ORDER BY report_month DESC
    """
    with _connect_readonly(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]

    for row in rows:
        row["is_anomaly"] = bool(row["z_score"] >= z_threshold)
    if only_anomalies:
        rows = [r for r in rows if r["is_anomaly"]]
    return rows


def get_fair_metadata(
    *,
    table_name: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return FAIR provenance, license, owner, schema version, and DQ status."""
    clauses: list[str] = []
    params: list[Any] = []
    if table_name:
        clauses.append("table_name = ?")
        params.append(table_name.strip())

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT table_name, dataset_id, schema_version, owner, license,
               source_url, source_sha256, row_count, dq_passed, updated_at_utc
        FROM fair_metadata
        {where_sql}
        ORDER BY table_name ASC
    """
    with _connect_readonly(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for row in rows:
        row["dq_passed"] = bool(row["dq_passed"])
    return rows
