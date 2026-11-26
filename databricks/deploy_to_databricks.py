"""Deploy the FAIR notebook and execute Unity Catalog tables with tags & Delta constraints."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = ROOT_DIR / "data" / "pharma_qih.db"
NOTEBOOK_LOCAL = ROOT_DIR / "databricks" / "fair_openfda_pipeline.py"
NOTEBOOK_REMOTE = "/Users/ppadwal8@gatech.edu/pharma_quality_intelligence_hub/fair_openfda_pipeline"
WAREHOUSE_ID = "fa180955ae034b8a"
PROFILE = "free"


def run_cli(args: list[str]) -> str:
    """Run a Databricks CLI command and return stdout."""
    proc = subprocess.run(
        ["databricks", *args, "--profile", PROFILE],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def exec_sql(statement: str) -> dict:
    """Execute one SQL statement on the Databricks Serverless SQL Warehouse."""
    payload = {
        "warehouse_id": WAREHOUSE_ID,
        "statement": statement,
        "wait_timeout": "50s",
    }
    out = run_cli(["api", "post", "/api/2.0/sql/statements", "--json", json.dumps(payload)])
    result = json.loads(out)
    state = result.get("status", {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"Databricks SQL failed ({state}): {result}")
    return result


def sql_escape(val: str) -> str:
    """Escape single quotes for SQL string literals."""
    return val.replace("'", "''")


def main() -> None:
    """Upload the PySpark notebook and publish governed FAIR Gold tables in Unity Catalog."""
    if not DB_PATH.exists():
        print("Local Gold DB missing. Run the local pipeline first.", file=sys.stderr)
        sys.exit(1)

    # 1. Upload the PySpark medallion notebook
    run_cli(["workspace", "mkdirs", "/Users/ppadwal8@gatech.edu/pharma_quality_intelligence_hub"])
    run_cli(
        [
            "workspace",
            "import",
            NOTEBOOK_REMOTE,
            "--file",
            str(NOTEBOOK_LOCAL),
            "--language",
            "PYTHON",
            "--format",
            "SOURCE",
            "--overwrite",
        ]
    )
    print(f"Uploaded notebook to {NOTEBOOK_REMOTE}")

    # 2. Create Unity Catalog schema and apply searchable Unity Catalog Tags
    exec_sql(
        "CREATE SCHEMA IF NOT EXISTS workspace.pharma_quality_fair "
        "COMMENT 'FAIR Pharma Quality Data Product built from OpenFDA recall data'"
    )
    exec_sql(
        "ALTER SCHEMA workspace.pharma_quality_fair SET TAGS ("
        "'data_product' = 'pharma_quality_recalls', "
        "'domain' = 'pharma_quality', "
        "'fair_compliant' = 'true')"
    )

    # 3. Create Gold recalls table with NOT NULL, PK, full contract fields, and FAIR TBLPROPERTIES
    exec_sql(
        """
        CREATE OR REPLACE TABLE workspace.pharma_quality_fair.gold_recalls (
            recall_number STRING NOT NULL COMMENT 'Persistent FDA recall identifier',
            recalling_firm STRING NOT NULL COMMENT 'Manufacturer or distributor initiating recall',
            classification STRING NOT NULL COMMENT 'FDA risk class per 21 CFR 7.3',
            status STRING NOT NULL COMMENT 'Recall lifecycle status',
            report_date DATE NOT NULL COMMENT 'ISO-8601 recall report date',
            report_month STRING NOT NULL COMMENT 'YYYY-MM bucket for anomaly monitoring',
            root_cause_category STRING NOT NULL COMMENT 'Standardized Pharma Quality root-cause taxonomy',
            reason_for_recall STRING NOT NULL COMMENT 'Official FDA reason text',
            product_description STRING NOT NULL COMMENT 'Official FDA product description',
            state STRING COMMENT 'US state code',
            country STRING NOT NULL COMMENT 'Country of recalling firm',
            ingested_at_utc STRING NOT NULL COMMENT 'UTC ingestion timestamp',
            source_sha256 STRING NOT NULL COMMENT 'SHA-256 checksum of raw Bronze payload',
            CONSTRAINT pk_gold_recalls PRIMARY KEY (recall_number)
        )
        USING DELTA
        COMMENT 'FAIR Gold table of normalized FDA drug recalls'
        TBLPROPERTIES (
            'fair.dataset_id' = 'urn:pharma-qih:dataset:gold_recalls:v1.0.0',
            'fair.schema_version' = '1.0.0',
            'fair.owner' = 'Pankaj Padwal',
            'fair.license' = 'CC0-1.0 / MIT',
            'fair.classification' = 'Public Regulatory Data',
            'fair.refresh_cadence' = 'daily',
            'fair.source' = 'https://api.fda.gov/drug/enforcement.json'
        )
        """
    )

    # Add enforced Delta CHECK constraints and Unity Catalog table tags
    exec_sql(
        "ALTER TABLE workspace.pharma_quality_fair.gold_recalls "
        "ADD CONSTRAINT valid_classification "
        "CHECK (classification IN ('Class I', 'Class II', 'Class III', 'Not Yet Classified'))"
    )
    exec_sql(
        "ALTER TABLE workspace.pharma_quality_fair.gold_recalls "
        "ADD CONSTRAINT valid_source_sha256 CHECK (length(source_sha256) = 64)"
    )
    exec_sql(
        "ALTER TABLE workspace.pharma_quality_fair.gold_recalls SET TAGS ("
        "'medallion_layer' = 'gold', 'classification' = 'public_regulatory', 'fair_owner' = 'pankaj_padwal')"
    )

    # 4. Load live Gold rows (with full product_description and untruncated reason_for_recall)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT recall_number, recalling_firm, classification, status,
                   report_date, report_month, root_cause_category,
                   reason_for_recall, product_description, state, country,
                   ingested_at_utc, source_sha256
            FROM gold_recalls
            ORDER BY report_date DESC
            LIMIT 100
            """
        ).fetchall()

    for i in range(0, len(rows), 25):
        chunk = rows[i : i + 25]
        values_sql = ",\n".join(
            "("
            f"'{sql_escape(r['recall_number'])}', "
            f"'{sql_escape(r['recalling_firm'])}', "
            f"'{sql_escape(r['classification'])}', "
            f"'{sql_escape(r['status'])}', "
            f"DATE '{sql_escape(r['report_date'])}', "
            f"'{sql_escape(r['report_month'])}', "
            f"'{sql_escape(r['root_cause_category'])}', "
            f"'{sql_escape(r['reason_for_recall'])}', "
            f"'{sql_escape(r['product_description'])}', "
            f"'{sql_escape(r['state'])}', "
            f"'{sql_escape(r['country'])}', "
            f"'{sql_escape(r['ingested_at_utc'])}', "
            f"'{sql_escape(r['source_sha256'])}'"
            ")"
            for r in chunk
        )
        exec_sql(f"INSERT INTO workspace.pharma_quality_fair.gold_recalls VALUES {values_sql}")

    # 5. Materialize Gold firm risk summary (including top_root_cause) with UC tags
    exec_sql(
        """
        CREATE OR REPLACE TABLE workspace.pharma_quality_fair.gold_firm_risk_summary
        USING DELTA
        COMMENT 'FAIR Gold firm risk summary weighted by recall severity'
        TBLPROPERTIES (
            'fair.dataset_id' = 'urn:pharma-qih:dataset:gold_firm_risk_summary:v1.0.0',
            'fair.schema_version' = '1.0.0',
            'fair.owner' = 'Pankaj Padwal',
            'fair.license' = 'CC0-1.0 / MIT'
        )
        AS
        WITH cause_rank AS (
            SELECT
                recalling_firm,
                root_cause_category,
                ROW_NUMBER() OVER (PARTITION BY recalling_firm ORDER BY COUNT(*) DESC, root_cause_category ASC) AS rn
            FROM workspace.pharma_quality_fair.gold_recalls
            GROUP BY recalling_firm, root_cause_category
        )
        SELECT
            r.recalling_firm,
            COUNT(*) AS total_recalls,
            SUM(CASE WHEN r.classification = 'Class I' THEN 1 ELSE 0 END) AS class_i_count,
            SUM(CASE WHEN r.classification = 'Class II' THEN 1 ELSE 0 END) AS class_ii_count,
            SUM(CASE WHEN r.classification = 'Class III' THEN 1 ELSE 0 END) AS class_iii_count,
            SUM(CASE WHEN r.classification = 'Class I' THEN 5 WHEN r.classification = 'Class II' THEN 2 ELSE 1 END) AS risk_score,
            MAX(c.root_cause_category) AS top_root_cause
        FROM workspace.pharma_quality_fair.gold_recalls r
        LEFT JOIN cause_rank c ON r.recalling_firm = c.recalling_firm AND c.rn = 1
        GROUP BY r.recalling_firm
        ORDER BY risk_score DESC, total_recalls DESC
        """
    )
    exec_sql(
        "ALTER TABLE workspace.pharma_quality_fair.gold_firm_risk_summary SET TAGS ("
        "'medallion_layer' = 'gold', 'classification' = 'public_regulatory', 'fair_owner' = 'pankaj_padwal')"
    )

    verify = exec_sql(
        "SELECT COUNT(*) AS recalls, COUNT(DISTINCT recalling_firm) AS firms "
        "FROM workspace.pharma_quality_fair.gold_recalls"
    )
    print("Databricks Unity Catalog verification:", json.dumps(verify.get("result", {}).get("data_array")))


if __name__ == "__main__":
    main()
