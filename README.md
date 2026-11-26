# Pharma Quality Intelligence Hub

A production-style **FAIR Pharma Quality Data Product**, **Python MCP Server**, **FastAPI REST API**, and **Evaluated Quality Investigation Agent** built on public FDA Drug Enforcement (Recall) data and deployed to **Databricks Unity Catalog**.

## Architecture

1. **FAIR Medallion Pipeline (`src/pharma_qih/pipeline/`, `databricks/`, `metadata/`)**
   - Ingests live OpenFDA drug recall records into Bronze, Silver, and Gold layers with SHA-256 source lineage, standardized Pharma Quality root-cause taxonomy, automated data-quality gates, and monthly Z-score anomaly detection.
   - Publishes Gold Delta tables (`gold_recalls`, `gold_firm_risk_summary`) into Databricks Unity Catalog (`workspace.pharma_quality_fair`) with FAIR `TBLPROPERTIES` and column comments.
   - Versioned JSON Schema contract (`metadata/data_contract.json`) and RBAC policy (`metadata/access_policy.yaml`).
2. **Custom Python MCP Server (`src/pharma_qih/mcp_server.py`)**
   - Built on the official Python `mcp` SDK (`FastMCP`) exposing 5 governed, read-only tools: `mcp_search_recalls`, `mcp_get_firm_risk_summary`, `mcp_get_root_cause_trends`, `mcp_get_monthly_anomalies`, and `mcp_get_fair_metadata`.
3. **Governed FastAPI REST Service (`src/pharma_qih/api.py`)**
   - Enforces API-key RBAC (`quality_viewer`, `quality_analyst`, `data_steward`) with Pydantic request validation and OpenAPI docs (`/docs`).
4. **AI Security, Monitoring & Golden Evaluation (`security.py`, `monitoring.py`, `evals/`)**
   - Blocks prompt-injection and SQL-injection payloads, redacts PII/tokens, logs run latency/citations/blocks to SQLite, and gates CI on a 12-case golden accuracy and adversarial security benchmark.

## Quickstart

```bash
uv run pharma-qih-pipeline --limit 250
uv run pytest -v
uv run pharma-qih-eval
uv run pharma-qih-api
```
