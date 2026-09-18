from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_generic_context import build_assessment, prepare_layout


SKILL_ROOT = Path(__file__).resolve().parents[1]
RENDERER = SKILL_ROOT / "scripts/render_context_report.py"
RENDER_SPEC = importlib.util.spec_from_file_location("context_report_renderer", RENDERER)
if RENDER_SPEC is None or RENDER_SPEC.loader is None:
    raise RuntimeError("cannot load render_context_report.py")
RENDER_MODULE = importlib.util.module_from_spec(RENDER_SPEC)
RENDER_SPEC.loader.exec_module(RENDER_MODULE)


def render(
    repo: Path, output: Path, assessment: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "python3", str(RENDERER), "--repo", str(repo), "--output", str(output),
    ]
    if assessment is not None:
        assessment_path = output.parent / "assessment.json"
        assessment_path.write_text(
            json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        command.extend(("--assessment", str(assessment_path)))
    return subprocess.run(command, check=False, capture_output=True, text=True)


class ContextReportRendererTests(unittest.TestCase):
    def test_renders_standard_schema_dimensions_coverage_and_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "guide.md", "checks/context.py")
            result = RENDER_MODULE.CONTEXT_KIT_MODULE.assess(repo)
            result.update({
                "schema_version": 5,
                "assessment_mode": "standard",
                "coverage": {"reviewed": 12, "total": 14, "ratio": 0.857},
                "confidence_summary": "medium",
                "score": 82,
                "dimensions": {"entry_routing": 9},
                "derived_levels": {"entry_routing": 3},
                "documentation": {
                    "score": 75,
                    "dimensions": {"service_ownership": 8},
                    "derived_levels": {"service_ownership": 2},
                    "findings": [],
                },
                "dimension_assessments": {
                    "context_control_plane": {
                        "entry_routing": {
                            "label": "Entry routing",
                            "level": 3,
                            "confidence": "high",
                            "rationale": "Agent verified the bounded entry and routing contract.",
                            "evidence": [{
                                "kind": "file", "path": "guide.md",
                                "statement": "Repository entry is explicit.",
                            }],
                            "limitations": ["Owner acceptance remains pending."],
                        },
                    },
                    "repository_documentation": {
                        "service_ownership": {
                            "label": "Service ownership",
                            "level": 2,
                            "confidence": "medium",
                            "rationale": "Ownership sources cover the sampled service.",
                            "evidence": ["catalog/owners.yaml"],
                            "limitations": ["Coverage excludes archived components."],
                        },
                    },
                },
            })
            html = RENDER_MODULE.render(result, repo)

        self.assertIn("Standard dimension assessment", html)
        self.assertIn("Schema</dt><dd><code>5</code>", html)
        self.assertIn("Coverage</dt><dd>12 / 14 (85.7%)", html)
        self.assertIn("Confidence</dt><dd>medium", html)
        self.assertIn("Entry routing", html)
        self.assertIn("L3", html)
        self.assertIn("high", html)
        self.assertIn("Agent verified the bounded entry and routing contract", html)
        self.assertIn("file: guide.md · Repository entry is explicit", html)
        self.assertIn("Owner acceptance remains pending", html)
        self.assertIn("Service ownership", html)
        self.assertIn("catalog/owners.yaml", html)
        self.assertIn("Coverage excludes archived components", html)
        self.assertNotIn("Assurance artifact 绑定", html)

    def test_renders_unassessed_tmr_without_inventing_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "guide.md", "checks/context.py")
            output = Path(directory) / "context-readiness-report.html"
            result = render(repo, output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            html = output.read_text(encoding="utf-8")

        self.assertIn("Context Readiness &amp; Remediation Report", html)
        self.assertIn("当前内部自评状态：待 Agent 语义自评", html)
        self.assertIn("待评测", html)
        self.assertIn("尚未由 Agent 核查", html)
        self.assertIn("尚未完成 Agent Assessment，证据组合未进入等级判定", html)
        self.assertIn("当前结果未包含已核验的 Validator/测试回执", html)
        self.assertIn("当前结果未包含 Assessment 引用的逐项 Git path + commit 证据", html)
        self.assertIn("Agent-assisted self-assessment", html)
        self.assertIn("Not certified", html)
        self.assertIn("Schema</dt><dd><code>4</code>", html)
        self.assertIn("Assurance v4 observation assessment", html)
        self.assertIn("Coverage</dt><dd>0 / 56 (0.0%)", html)
        self.assertIn("Confidence</dt><dd>未单独声明", html)
        self.assertIn("Assurance artifact 绑定", html)
        self.assertIn("Standard Assessment 绑定", html)
        self.assertIn("独立 AI 复核", html)
        self.assertNotIn("外部认证结论", html)
        self.assertNotIn("https://cdn.", html)

    def test_renders_provisional_tmr_from_complete_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            assessment, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="pass",
            )
            output = Path(directory) / "context-readiness-report.html"
            result = render(repo, output, assessment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            html = output.read_text(encoding="utf-8")

        self.assertIn("当前内部自评状态：内部自评暂定达标（Provisional）", html)
        self.assertIn("100 / 100", html)
        self.assertIn("L4 / 4", html)
        self.assertIn("成熟度由四个固定观察项推导", html)
        self.assertIn("context/entry.md", html)
        self.assertIn("不是外部认证、审计意见或绩效 KPI", html)
        self.assertNotIn("当前内部自评状态：待 Agent 语义自评", html)

    def test_owner_acceptance_state_is_not_presented_as_certification(self) -> None:
        label, explanation = RENDER_MODULE.gate_presentation(
            "ready_for_owner_acceptance"
        )

        self.assertEqual(label, "可提交 Owner 验收（尚未认证）")
        self.assertIn("不等于 Owner 已接受", explanation)
        self.assertIn("不是外部认证", explanation)

    def test_renders_grade_receipt_and_git_evidence_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "guide.md", "checks/context.py")
            result = RENDER_MODULE.CONTEXT_KIT_MODULE.assess(repo)
            result.update({
                "evidence_grade": "B",
                "evidence_grade_reason": "文件、Git 与可执行证据已形成组合。",
                "assurance_bindings": {
                    "ready": True,
                    "standard_assessment": {
                        "status": "valid", "actual_sha256": "1" * 64,
                        "coverage": {"percent": 100}, "score": 90,
                        "documentation_score": 91, "gate_status": "provisional",
                        "run_id": "standard-run",
                    },
                    "independent_ai_review": {
                        "status": "valid", "actual_sha256": "2" * 64,
                        "reviewer_id": "review-agent", "run_id": "review-run",
                        "decision": "approved", "reviewed_at": "2026-09-01T00:00:00Z",
                        "standard_assessment_sha256": "1" * 64,
                        "assurance_core_sha256": "3" * 64,
                    },
                    "errors": [],
                    "limitation": "不同 run_id 的本地记录不证明现实身份独立。",
                },
                "receipt_evidence": [{
                    "claim": "context.entry_routing.entry_exists",
                    "kind": "validator",
                    "command": ["python3", "checks/context.py", "stable-fragment"],
                    "artifact_path": "/local/evidence/receipt.json",
                    "artifact_sha256": "a" * 64,
                    "execution_fingerprint": "b" * 64,
                    "input_hashes": [{"path": "checks/context.py", "sha256": "c" * 64}],
                    "stdout_sha256": "d" * 64,
                    "stderr_sha256": "e" * 64,
                    "selector": {"kind": "substring", "expected": "stable-fragment", "matched": True},
                    "exit_code": 0,
                    "recorded_at": "2026-08-31T01:02:03+00:00",
                    "duration_ms": 17,
                }],
                "git_assessment_evidence": [{
                    "observation_id": "documentation.ownership.owner_source",
                    "path": "guide.md",
                    "commit": "f" * 40,
                    "statement": "核查正式知识入口",
                    "proves": "证明该文件的当前 Git 来源。",
                    "does_not_prove": "不能据此推断 Owner 身份。",
                }],
            })
            html = RENDER_MODULE.render(result, repo)

        self.assertIn("证据等级 B", html)
        self.assertIn("文件、Git 与可执行证据已形成组合", html)
        self.assertIn("context.entry_routing.entry_exists", html)
        self.assertIn("python3 checks/context.py stable-fragment", html)
        self.assertIn("/local/evidence/receipt.json", html)
        self.assertIn("a" * 64, html)
        self.assertIn("b" * 64, html)
        self.assertIn("checks/context.py: " + "c" * 64, html)
        self.assertIn("stdout: " + "d" * 64, html)
        self.assertIn("stderr: " + "e" * 64, html)
        self.assertIn("stable-fragment", html)
        self.assertIn("2026-08-31T01:02:03+00:00", html)
        self.assertIn("17 ms", html)
        self.assertIn("documentation.ownership.owner_source", html)
        self.assertIn("f" * 40, html)
        self.assertIn("不能据此推断 Owner 身份", html)
        self.assertIn("Standard Assessment 绑定", html)
        self.assertIn("review-agent", html)
        self.assertIn("approved", html)
        self.assertIn("3" * 64, html)

    def test_evidence_entries_remain_renderable_when_optional_fields_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "guide.md", "checks/context.py")
            result = RENDER_MODULE.CONTEXT_KIT_MODULE.assess(repo)
            result["receipt_evidence"] = [{"claim": "claim.with.partial.fields"}]
            result["git_assessment_evidence"] = [{"path": "guide.md"}]
            result.pop("measurement_type", None)
            result.pop("certification_status", None)
            html = RENDER_MODULE.render(result, repo)

        self.assertIn("claim.with.partial.fields", html)
        self.assertIn("guide.md", html)
        self.assertIn("未记录", html)
        self.assertIn("Agent-assisted self-assessment", html)
        self.assertIn("Not certified", html)


if __name__ == "__main__":
    unittest.main()
