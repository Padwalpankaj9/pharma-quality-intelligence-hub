"""Central configuration and paths for Pharma Quality Intelligence Hub."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("PHARMA_QIH_DATA_DIR", str(ROOT_DIR / "data")))
METADATA_DIR = ROOT_DIR / "metadata"
DB_PATH = Path(os.getenv("PHARMA_QIH_DB_PATH", str(DATA_DIR / "pharma_qih.db")))

OPENFDA_URL = "https://api.fda.gov/drug/enforcement.json"
FAIR_SCHEMA_VERSION = "1.0.0"
DATA_OWNER = "Pankaj Padwal"

# Default local development keys map to RBAC roles; override via env in production
DEFAULT_API_KEYS: dict[str, str] = {
    os.getenv("PHARMA_QIH_VIEWER_KEY", "dev-viewer-key"): "quality_viewer",
    os.getenv("PHARMA_QIH_ANALYST_KEY", "dev-analyst-key"): "quality_analyst",
    os.getenv("PHARMA_QIH_STEWARD_KEY", "dev-steward-key"): "data_steward",
}
