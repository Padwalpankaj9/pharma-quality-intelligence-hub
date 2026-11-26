"""Bronze -> Silver -> Gold FAIR data pipeline for OpenFDA drug recall data."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from pharma_qih.config import (
    DATA_DIR,
    DATA_OWNER,
    DB_PATH,
    FAIR_SCHEMA_VERSION,
    METADATA_DIR,
    OPENFDA_URL,
)

LOGGER = logging.getLogger(__name__)
VALID_CLASSES = {"Class I", "Class II", "Class III", "Not Yet Classified"}

# Built-in fallback records keep tests and offline runs deterministic
FALLBACK_RECORDS: list[dict[str, Any]] = [
    {
        "recall_number": "D-0101-2026",
        "recalling_firm": "Apex Sterile Labs LLC",
        "classification": "Class I",
        "status": "Ongoing",
        "report_date": "20260115",
        "reason_for_recall": "Lack of sterility assurance in injectable vials.",
        "product_description": "Sterile Sodium Chloride Injection 0.9%, 10 mL vials",
        "state": "NJ",
        "country": "United States",
    },
    {
        "recall_number": "D-0102-2026",
        "recalling_firm": "Apex Sterile Labs LLC",
        "classification": "Class I",
        "status": "Ongoing",
        "report_date": "20260122",
        "reason_for_recall": "Microbial contamination identified during sterility testing.",
        "product_description": "Cefazolin for Injection USP, 1 g vials",
        "state": "NJ",
        "country": "United States",
    },
    {
        "recall_number": "D-0103-2026",
        "recalling_firm": "Midwest Pharma Corp",
        "classification": "Class II",
        "status": "Completed",
        "report_date": "20260210",
        "reason_for_recall": "Subpotent drug: failed stability assay at 12 months.",
        "product_description": "Metformin HCl Extended-Release Tablets, 500 mg",
        "state": "IL",
        "country": "United States",
    },
    {
        "recall_number": "D-0104-2026",
        "recalling_firm": "Global Generics Inc",
        "classification": "Class II",
        "status": "Ongoing",
        "report_date": "20260218",
        "reason_for_recall": "Presence of nitrosamine impurity above acceptable intake limit.",
        "product_description": "Valsartan Tablets USP, 160 mg",
        "state": "CA",
        "country": "United States",
    },
    {
        "recall_number": "D-0105-2026",
        "recalling_firm": "Pacific Packaging Co",
        "classification": "Class III",
        "status": "Completed",
        "report_date": "20260305",
        "reason_for_recall": "Labeling: incorrect expiration date printed on carton.",
        "product_description": "Atorvastatin Calcium Tablets, 20 mg, 90-count bottle",
        "state": "CA",
        "country": "United States",
    },
    {
        "recall_number": "D-0106-2026",
        "recalling_firm": "Apex Sterile Labs LLC",
        "classification": "Class II",
        "status": "Ongoing",
        "report_date": "20260319",
        "reason_for_recall": "CGMP deviations: inadequate environmental monitoring in aseptic suite.",
        "product_description": "Heparin Sodium Injection, 5,000 USP units/mL",
        "state": "NJ",
        "country": "United States",
    },
    {
        "recall_number": "D-0107-2026",
        "recalling_firm": "Summit Biologics Inc",
        "classification": "Class II",
        "status": "Ongoing",
        "report_date": "20260408",
        "reason_for_recall": "Visible glass particulate matter found in single-dose vial.",
        "product_description": "Monoclonal Antibody Solution for Infusion, 100 mg/4 mL",
        "state": "MA",
        "country": "United States",
    },
    {
        "recall_number": "D-0108-2026",
        "recalling_firm": "Midwest Pharma Corp",
        "classification": "Class II",
        "status": "Ongoing",
        "report_date": "20260414",
        "reason_for_recall": "Failed dissolution specifications during routine stability.",
        "product_description": "Losartan Potassium Tablets, 50 mg",
        "state": "IL",
        "country": "United States",
    },
]


def load_data_contract() -> dict[str, Any]:
    """Load the machine-readable FAIR JSON Schema data contract."""
    contract_path = METADATA_DIR / "data_contract.json"
    return json.loads(contract_path.read_text(encoding="utf-8"))


def classify_root_cause(reason: str) -> str:
    """Group free-text FDA recall reasons into a standard pharma Quality taxonomy."""
    text = reason.lower()
    if any(k in text for k in ("steril", "microb", "contaminat", "endotoxin", "fungal", "bacterial")):
        return "Sterility & Contamination"
    if any(k in text for k in ("nitrosamine", "impurit", "degrad", "benzene", "ndma")):
        return "Impurity & Nitrosamine"
    if any(k in text for k in ("poten", "assay", "stabilit", "dissolution", "superpotent", "subpotent")):
        return "Potency, Assay & Stability"
    if any(k in text for k in ("cgmp", "gmp", "good manufacturing", "validation", "environmental monitoring")):
        return "CGMP Deviations"
    if any(k in text for k in ("particulate", "foreign", "glass", "metal", "fiber")):
        return "Foreign Matter & Particulate"
    if any(k in text for k in ("label", "packag", "misbrand", "carton", "expiration", "wrong strength")):
        return "Labeling & Packaging"
    return "Other Quality Defect"


def parse_fda_date(raw_date: str | None) -> str:
    """Convert YYYYMMDD FDA strings into ISO YYYY-MM-DD format."""
    if not raw_date or len(raw_date) != 8 or not raw_date.isdigit():
        return "1970-01-01"
    return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"


def fetch_openfda_recalls(limit: int = 250, offline: bool = False) -> tuple[list[dict[str, Any]], str]:
    """Download recent FDA drug recalls or use the built-in snapshot when offline."""
    if offline:
        return list(FALLBACK_RECORDS), "builtin_fallback"

    params = {"limit": min(limit, 1000), "sort": "report_date:desc"}
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(OPENFDA_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results", [])
            if results:
                return results, OPENFDA_URL
    except (httpx.HTTPError, ValueError) as exc:
        LOGGER.warning("OpenFDA request failed (%s); falling back to local snapshot.", exc)
    return list(FALLBACK_RECORDS), "builtin_fallback"


def run_quality_checks(silver_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Enforce JSON Schema data contract and FAIR data-quality gates before Gold promotion."""
    total = len(silver_rows)
    if total == 0:
        raise ValueError("Data quality gate failed: zero Silver rows produced.")

    contract = load_data_contract()
    validator = jsonschema.Draft202012Validator(contract)
    schema_errors = sum(1 for row in silver_rows if not validator.is_valid(row))

    missing_ids = sum(1 for r in silver_rows if not r["recall_number"])
    bad_classes = sum(1 for r in silver_rows if r["classification"] not in VALID_CLASSES)
    bad_dates = sum(1 for r in silver_rows if r["report_date"] == "1970-01-01")
    duplicate_ids = total - len({r["recall_number"] for r in silver_rows})
    max_bad_date_ratio = float(contract.get("sla", {}).get("max_invalid_date_ratio", 0.05))
    bad_date_ratio = bad_dates / total

    checks = {
        "row_count": total,
        "schema_errors": schema_errors,
        "missing_recall_number": missing_ids,
        "invalid_classification": bad_classes,
        "invalid_report_date": bad_dates,
        "invalid_date_ratio": round(bad_date_ratio, 4),
        "duplicate_recall_number": duplicate_ids,
        "passed": (
            schema_errors == 0
            and missing_ids == 0
            and bad_classes == 0
            and duplicate_ids == 0
            and bad_date_ratio <= max_bad_date_ratio
        ),
    }
    if not checks["passed"]:
        raise ValueError(f"Data quality gate failed: {checks}")
    return checks


def compute_monthly_anomalies(silver_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag months with unusually high recall volume using a simple Z-score."""
    counts = Counter(r["report_month"] for r in silver_rows if r["report_month"] != "1970-01")
    if not counts:
        return []
    months = sorted(counts.keys())
    values = [counts[m] for m in months]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(variance)

    anomalies: list[dict[str, Any]] = []
    for month in months:
        count = counts[month]
        z_score = round((count - mean) / std, 2) if std > 0 else 0.0
        anomalies.append(
            {
                "report_month": month,
                "recall_count": count,
                "mean_monthly_recalls": round(mean, 2),
                "z_score": z_score,
                "is_anomaly": int(z_score >= 1.5),
            }
        )
    return anomalies


def build_pipeline(
    *,
    limit: int = 250,
    offline: bool = False,
    db_path: Path = DB_PATH,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    """Execute the full Bronze -> Silver -> Gold FAIR pipeline and persist to SQLite."""
    contract = load_data_contract()
    dataset_pid = str(contract.get("$id", "urn:pharma-qih:dataset:gold_recalls:v1.0.0"))
    license_info = contract.get("license", {})
    license_label = f"{license_info.get('source_license', 'CC0-1.0')} / {license_info.get('derived_product_license', 'MIT')}"

    raw_records, source_used = fetch_openfda_recalls(limit=limit, offline=offline)
    ingested_at = datetime.now(UTC).isoformat()
    raw_blob = json.dumps(raw_records, sort_keys=True).encode("utf-8")
    source_sha256 = hashlib.sha256(raw_blob).hexdigest()

    # Save raw Bronze snapshot for full audit lineage
    bronze_dir = data_dir / "bronze"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    bronze_file = bronze_dir / "openfda_recalls_latest.json"
    bronze_file.write_bytes(raw_blob)

    # Transform Bronze into deduplicated, normalized Silver rows
    seen: set[str] = set()
    silver_rows: list[dict[str, Any]] = []
    for rec in raw_records:
        recall_num = str(rec.get("recall_number") or "").strip()
        if not recall_num or recall_num in seen:
            continue
        seen.add(recall_num)

        classification = str(rec.get("classification") or "Not Yet Classified").strip()
        if classification not in VALID_CLASSES:
            classification = "Not Yet Classified"

        report_date = parse_fda_date(rec.get("report_date"))
        reason = str(rec.get("reason_for_recall") or "Not specified").strip()
        silver_rows.append(
            {
                "recall_number": recall_num,
                "recalling_firm": str(rec.get("recalling_firm") or "Unknown").strip(),
                "classification": classification,
                "status": str(rec.get("status") or "Unknown").strip(),
                "report_date": report_date,
                "report_month": report_date[:7],
                "root_cause_category": classify_root_cause(reason),
                "reason_for_recall": reason,
                "product_description": str(rec.get("product_description") or "Not specified").strip(),
                "state": str(rec.get("state") or "").strip(),
                "country": str(rec.get("country") or "Unknown").strip(),
                "ingested_at_utc": ingested_at,
                "source_sha256": source_sha256,
            }
        )

    dq_report = run_quality_checks(silver_rows)
    anomalies = compute_monthly_anomalies(silver_rows)

    # Compute Gold firm risk summary
    firm_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in silver_rows:
        firm_groups[row["recalling_firm"]].append(row)

    firm_rows: list[dict[str, Any]] = []
    for firm, items in firm_groups.items():
        c1 = sum(1 for i in items if i["classification"] == "Class I")
        c2 = sum(1 for i in items if i["classification"] == "Class II")
        c3 = sum(1 for i in items if i["classification"] == "Class III")
        # Weight Class I highest because it carries the greatest patient safety risk
        risk_score = c1 * 5 + c2 * 2 + c3 * 1
        top_cause = Counter(i["root_cause_category"] for i in items).most_common(1)[0][0]
        firm_rows.append(
            {
                "recalling_firm": firm,
                "total_recalls": len(items),
                "class_i_count": c1,
                "class_ii_count": c2,
                "class_iii_count": c3,
                "risk_score": risk_score,
                "top_root_cause": top_cause,
            }
        )
    firm_rows.sort(key=lambda x: (-x["risk_score"], -x["total_recalls"], x["recalling_firm"]))

    # Compute Gold root-cause trends by year and category
    trend_counts: Counter[tuple[str, str]] = Counter(
        (r["report_date"][:4], r["root_cause_category"]) for r in silver_rows
    )
    trend_rows = [
        {"report_year": year, "root_cause_category": cat, "recall_count": cnt}
        for (year, cat), cnt in sorted(trend_counts.items())
    ]

    # Write Gold tables and FAIR metadata into SQLite
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS gold_recalls;
            DROP TABLE IF EXISTS gold_firm_risk_summary;
            DROP TABLE IF EXISTS gold_root_cause_trends;
            DROP TABLE IF EXISTS gold_monthly_anomalies;
            DROP TABLE IF EXISTS fair_metadata;

            CREATE TABLE gold_recalls (
                recall_number TEXT PRIMARY KEY,
                recalling_firm TEXT NOT NULL,
                classification TEXT NOT NULL,
                status TEXT NOT NULL,
                report_date TEXT NOT NULL,
                report_month TEXT NOT NULL,
                root_cause_category TEXT NOT NULL,
                reason_for_recall TEXT NOT NULL,
                product_description TEXT NOT NULL,
                state TEXT NOT NULL,
                country TEXT NOT NULL,
                ingested_at_utc TEXT NOT NULL,
                source_sha256 TEXT NOT NULL
            );

            CREATE TABLE gold_firm_risk_summary (
                recalling_firm TEXT PRIMARY KEY,
                total_recalls INTEGER NOT NULL,
                class_i_count INTEGER NOT NULL,
                class_ii_count INTEGER NOT NULL,
                class_iii_count INTEGER NOT NULL,
                risk_score INTEGER NOT NULL,
                top_root_cause TEXT NOT NULL
            );

            CREATE TABLE gold_root_cause_trends (
                report_year TEXT NOT NULL,
                root_cause_category TEXT NOT NULL,
                recall_count INTEGER NOT NULL,
                PRIMARY KEY (report_year, root_cause_category)
            );

            CREATE TABLE gold_monthly_anomalies (
                report_month TEXT PRIMARY KEY,
                recall_count INTEGER NOT NULL,
                mean_monthly_recalls REAL NOT NULL,
                z_score REAL NOT NULL,
                is_anomaly INTEGER NOT NULL
            );

            CREATE TABLE fair_metadata (
                table_name TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                owner TEXT NOT NULL,
                license TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                dq_passed INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            """
        )

        conn.executemany(
            """
            INSERT INTO gold_recalls VALUES (
                :recall_number, :recalling_firm, :classification, :status,
                :report_date, :report_month, :root_cause_category, :reason_for_recall,
                :product_description, :state, :country, :ingested_at_utc, :source_sha256
            )
            """,
            silver_rows,
        )
        conn.executemany(
            """
            INSERT INTO gold_firm_risk_summary VALUES (
                :recalling_firm, :total_recalls, :class_i_count, :class_ii_count,
                :class_iii_count, :risk_score, :top_root_cause
            )
            """,
            firm_rows,
        )
        conn.executemany(
            """
            INSERT INTO gold_root_cause_trends VALUES (
                :report_year, :root_cause_category, :recall_count
            )
            """,
            trend_rows,
        )
        conn.executemany(
            """
            INSERT INTO gold_monthly_anomalies VALUES (
                :report_month, :recall_count, :mean_monthly_recalls, :z_score, :is_anomaly
            )
            """,
            anomalies,
        )

        meta_entries = [
            ("gold_recalls", len(silver_rows)),
            ("gold_firm_risk_summary", len(firm_rows)),
            ("gold_root_cause_trends", len(trend_rows)),
            ("gold_monthly_anomalies", len(anomalies)),
        ]
        conn.executemany(
            """
            INSERT INTO fair_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    tbl,
                    f"urn:pharma-qih:dataset:{tbl}:v{FAIR_SCHEMA_VERSION}",
                    FAIR_SCHEMA_VERSION,
                    DATA_OWNER,
                    license_label,
                    source_used,
                    source_sha256,
                    cnt,
                    int(dq_report["passed"]),
                    ingested_at,
                )
                for tbl, cnt in meta_entries
            ],
        )
        conn.commit()

    return {
        "dataset_id": dataset_pid,
        "license": license_label,
        "source": source_used,
        "source_sha256": source_sha256,
        "ingested_at_utc": ingested_at,
        "dq_report": dq_report,
        "tables": {
            "gold_recalls": len(silver_rows),
            "gold_firm_risk_summary": len(firm_rows),
            "gold_root_cause_trends": len(trend_rows),
            "gold_monthly_anomalies": len(anomalies),
        },
    }


def main() -> None:
    """CLI entrypoint to refresh the local FAIR Gold data product."""
    parser = argparse.ArgumentParser(description="Run the OpenFDA FAIR data pipeline.")
    parser.add_argument("--limit", type=int, default=250, help="Number of recalls to fetch.")
    parser.add_argument("--offline", action="store_true", help="Use built-in fallback dataset.")
    args = parser.parse_args()
    summary = build_pipeline(limit=args.limit, offline=args.offline)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
