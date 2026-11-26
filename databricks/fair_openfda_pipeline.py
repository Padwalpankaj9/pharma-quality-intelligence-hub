# Databricks notebook source
# MAGIC %md
# MAGIC # Pharma Quality Intelligence Hub - FAIR Medallion Pipeline (Unity Catalog)
# MAGIC
# MAGIC Ingests public OpenFDA Drug Enforcement (Recall) data into Bronze, Silver, and Gold
# MAGIC Delta tables inside `workspace.pharma_quality_fair` with Unity Catalog `SET TAGS`,
# MAGIC enforced Delta `NOT NULL`/`CHECK` constraints, FAIR metadata, and column comments.

# COMMAND ----------
import hashlib
import json
import urllib.request
from datetime import datetime, timezone

from pyspark.sql import functions as F

CATALOG = "workspace"
SCHEMA = "pharma_quality_fair"
OPENFDA_URL = "https://api.fda.gov/drug/enforcement.json?limit=250&sort=report_date:desc"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA} COMMENT 'FAIR Pharma Quality Data Product from OpenFDA'")
spark.sql(
    f"ALTER SCHEMA {CATALOG}.{SCHEMA} SET TAGS ("
    "'data_product' = 'pharma_quality_recalls', "
    "'domain' = 'pharma_quality', "
    "'fair_compliant' = 'true')"
)

# COMMAND ----------
# Pull live OpenFDA recall records and compute a SHA-256 checksum for lineage
with urllib.request.urlopen(OPENFDA_URL, timeout=30) as resp:
    raw_bytes = resp.read()

source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
ingested_at = datetime.now(timezone.utc).isoformat()
records = json.loads(raw_bytes.decode("utf-8")).get("results", [])

bronze_rows = [
    {
        "recall_number": str(r.get("recall_number") or ""),
        "recalling_firm": str(r.get("recalling_firm") or "Unknown"),
        "classification": str(r.get("classification") or "Not Yet Classified"),
        "status": str(r.get("status") or "Unknown"),
        "report_date_raw": str(r.get("report_date") or ""),
        "reason_for_recall": str(r.get("reason_for_recall") or "Not specified"),
        "product_description": str(r.get("product_description") or "Not specified"),
        "state": str(r.get("state") or ""),
        "country": str(r.get("country") or "Unknown"),
        "ingested_at_utc": ingested_at,
        "source_sha256": source_sha256,
    }
    for r in records
]

bronze_df = spark.createDataFrame(bronze_rows)
bronze_df.write.mode("overwrite").saveAsTable(f"{CATALOG}.{SCHEMA}.bronze_recalls")

# COMMAND ----------
# Clean, deduplicate, normalize ISO dates, and classify root causes using aligned taxonomy
silver_df = (
    spark.table(f"{CATALOG}.{SCHEMA}.bronze_recalls")
    .filter(F.length(F.trim(F.col("recall_number"))) > 0)
    .dropDuplicates(["recall_number"])
    .withColumn("report_date", F.to_date(F.col("report_date_raw"), "yyyyMMdd"))
    .withColumn("report_month", F.date_format(F.col("report_date"), "yyyy-MM"))
    .withColumn("reason_lower", F.lower(F.col("reason_for_recall")))
    .withColumn(
        "root_cause_category",
        F.when(F.col("reason_lower").rlike("steril|microb|contaminat|endotoxin|fungal|bacterial"), "Sterility & Contamination")
        .when(F.col("reason_lower").rlike("nitrosamine|impurit|degrad|benzene|ndma"), "Impurity & Nitrosamine")
        .when(F.col("reason_lower").rlike("poten|assay|stabilit|dissolution|superpotent|subpotent"), "Potency, Assay & Stability")
        .when(F.col("reason_lower").rlike("cgmp|gmp|good manufacturing|validation|environmental monitoring"), "CGMP Deviations")
        .when(F.col("reason_lower").rlike("particulate|foreign|glass|metal|fiber"), "Foreign Matter & Particulate")
        .when(F.col("reason_lower").rlike("label|packag|misbrand|carton|expiration|wrong strength"), "Labeling & Packaging")
        .otherwise("Other Quality Defect"),
    )
    .drop("reason_lower", "report_date_raw")
)

# Automated data-quality gate before promoting to Gold
row_count = silver_df.count()
null_ids = silver_df.filter(F.col("recall_number").isNull()).count()
assert row_count > 0 and null_ids == 0, f"FAIR DQ gate failed: rows={row_count}, null_ids={null_ids}"

silver_df.write.mode("overwrite").saveAsTable(f"{CATALOG}.{SCHEMA}.silver_recalls")

# COMMAND ----------
# Create Gold table explicitly (not CTAS) so NOT NULL, PK, CHECK constraints, and column comments persist
spark.sql(f"""
CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.gold_recalls (
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
COMMENT 'FAIR Gold table of normalized FDA drug recalls with SHA-256 lineage.'
TBLPROPERTIES (
  'fair.dataset_id' = 'urn:pharma-qih:dataset:gold_recalls:v1.0.0',
  'fair.schema_version' = '1.0.0',
  'fair.owner' = 'Pankaj Padwal',
  'fair.license' = 'CC0-1.0 / MIT',
  'fair.refresh_cadence' = 'daily',
  'fair.source' = 'https://api.fda.gov/drug/enforcement.json'
)
""")
spark.sql(
    f"ALTER TABLE {CATALOG}.{SCHEMA}.gold_recalls ADD CONSTRAINT valid_classification "
    "CHECK (classification IN ('Class I', 'Class II', 'Class III', 'Not Yet Classified'))"
)
spark.sql(
    f"ALTER TABLE {CATALOG}.{SCHEMA}.gold_recalls ADD CONSTRAINT valid_source_sha256 "
    "CHECK (length(source_sha256) = 64)"
)
spark.sql(
    f"ALTER TABLE {CATALOG}.{SCHEMA}.gold_recalls SET TAGS ("
    "'medallion_layer' = 'gold', 'classification' = 'public_regulatory', 'fair_owner' = 'pankaj_padwal')"
)

spark.table(f"{CATALOG}.{SCHEMA}.silver_recalls").select(
    "recall_number", "recalling_firm", "classification", "status",
    "report_date", "report_month", "root_cause_category", "reason_for_recall",
    "product_description", "state", "country", "ingested_at_utc", "source_sha256",
).write.mode("append").saveAsTable(f"{CATALOG}.{SCHEMA}.gold_recalls")

print(f"Published {row_count} FAIR Gold recall records to {CATALOG}.{SCHEMA}.")
