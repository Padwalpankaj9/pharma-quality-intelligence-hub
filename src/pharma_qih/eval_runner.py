"""Golden evaluation, citation faithfulness, guardrail FPR, and RBAC benchmark runner."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from pharma_qih.agent import ask_quality_agent
from pharma_qih.config import ROOT_DIR
from pharma_qih.pipeline.run_pipeline import build_pipeline


def run_golden_eval(report_path: Path | None = None) -> dict[str, Any]:
    """Run stratified accuracy, FPR, RBAC, and adversarial security evals on an isolated DB."""
    cases_path = ROOT_DIR / "evals" / "golden_cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_path = tmp / "eval.db"
        build_pipeline(offline=True, db_path=db_path, data_dir=tmp)

        results: list[dict[str, Any]] = []
        tool_hits = 0
        tool_total = 0
        cit_tp = 0
        cit_pred = 0
        cit_expected = 0
        faithful_hits = 0
        faithful_total = 0
        benign_false_blocks = 0
        benign_total = 0
        rbac_leaks = 0

        for case in cases:
            role = case.get("role", "quality_analyst")
            out = ask_quality_agent(case["question"], role=role, db_path=db_path)
            passed = out["status"] == case["expected_status"]
            cat = case["category"]

            if cat in ("accuracy", "benign_tricky"):
                benign_total += 1
                if out["status"] == "blocked":
                    benign_false_blocks += 1

                tool_total += 1
                if case["expected_tool"] in out["tools_used"]:
                    tool_hits += 1
                else:
                    passed = False

                for token in case.get("must_contain", []):
                    if token not in out["answer"]:
                        passed = False

                expected_cits = set(case.get("expected_citations", []))
                actual_cits = set(out["citations"])
                if expected_cits:
                    cit_tp += len(expected_cits & actual_cits)
                    cit_pred += len(actual_cits)
                    cit_expected += len(expected_cits)
                    if not expected_cits.issubset(actual_cits):
                        passed = False

                # Check citation faithfulness: every recall ID cited must appear in the answer text
                for cit in actual_cits:
                    if cit.startswith("D-"):
                        faithful_total += 1
                        if cit in out["answer"]:
                            faithful_hits += 1
                        else:
                            passed = False

            elif cat == "rbac":
                if out["status"] != "blocked" or out["blocked_reason"] != case.get("expected_reason"):
                    passed = False
                    rbac_leaks += 1

            elif cat == "security" and out["blocked_reason"] != case.get("expected_reason"):
                passed = False

            results.append(
                {
                    "id": case["id"],
                    "category": cat,
                    "role": role,
                    "passed": passed,
                    "status": out["status"],
                    "blocked_reason": out["blocked_reason"],
                    "tools_used": out["tools_used"],
                    "citation_count": len(out["citations"]),
                }
            )

    acc_cases = [r for r in results if r["category"] in ("accuracy", "benign_tricky")]
    sec_cases = [r for r in results if r["category"] == "security"]
    rbac_cases = [r for r in results if r["category"] == "rbac"]

    report = {
        "total_cases": len(results),
        "tool_selection_accuracy": round((tool_hits / max(1, tool_total)) * 100, 1),
        "accuracy_pass_rate": round((sum(1 for r in acc_cases if r["passed"]) / len(acc_cases)) * 100, 1),
        "citation_precision": round((cit_tp / max(1, cit_pred)) * 100, 1),
        "citation_recall": round((cit_tp / max(1, cit_expected)) * 100, 1),
        "citation_faithfulness": round((faithful_hits / max(1, faithful_total)) * 100, 1),
        "guardrail_recall": round((sum(1 for r in sec_cases if r["passed"]) / len(sec_cases)) * 100, 1),
        "guardrail_fpr": round((benign_false_blocks / max(1, benign_total)) * 100, 1),
        "rbac_block_rate": round((sum(1 for r in rbac_cases if r["passed"]) / len(rbac_cases)) * 100, 1),
        "rbac_leak_count": rbac_leaks,
        "all_passed": all(r["passed"] for r in results) and benign_false_blocks == 0 and rbac_leaks == 0,
        "results": results,
    }

    target_report = report_path or (ROOT_DIR / "evals" / "latest_report.json")
    target_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    """CLI entrypoint that exits non-zero if any golden accuracy, FPR, or security gate fails."""
    report = run_golden_eval()
    print(json.dumps(report, indent=2))
    if not report["all_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
