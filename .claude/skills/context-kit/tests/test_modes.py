from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts/context_kit.py"
RENDERER = SKILL_ROOT / "scripts/render_context_report.py"
CONTEXT_KEYS = {
    "entry_routing",
    "routing_coverage",
    "authority_and_content",
    "rule_enforceability",
    "token_budget",
    "task_loop",
    "skill_governance",
    "lifecycle_consumption_isolation",
}
DOCUMENTATION_KEYS = {
    "inventory_classification",
    "service_ownership_and_entry",
    "engineering_hierarchy",
    "link_integrity",
    "derived_context_interfaces",
    "content_quality_and_freshness",
}


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class ContextKitModeTests(unittest.TestCase):
    def test_default_quick_is_bounded_markdown_with_eight_context_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            result = run("--repo", str(repo))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("# Context Kit 1.0 · Quick\n"))
        self.assertLessEqual(len(result.stdout.splitlines()), 80)
        self.assertLess(len(result.stdout.encode("utf-8")), 8192)
        self.assertNotIn('"schema_version"', result.stdout)
        for key in CONTEXT_KEYS:
            self.assertIn(f"`{key}`", result.stdout)
        for key in DOCUMENTATION_KEYS:
            self.assertNotIn(f"`{key}`", result.stdout)

    def test_standard_template_has_one_ai_judgement_per_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            template = Path(directory) / "standard.json"
            result = run(
                "--repo", str(repo), "--mode", "standard",
                "--write-template", str(template),
            )
            payload = json.loads(template.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["assessment_mode"], "standard")
        self.assertEqual(payload["product_version"], "1.0")
        self.assertEqual(
            set(payload["dimensions"]["context_control_plane"]), CONTEXT_KEYS,
        )
        self.assertEqual(
            set(payload["dimensions"]["repository_documentation"]),
            DOCUMENTATION_KEYS,
        )
        for dimensions in payload["dimensions"].values():
            for judgement in dimensions.values():
                self.assertEqual(
                    set(judgement),
                    {"level", "confidence", "rationale", "evidence", "limitations"},
                )
                self.assertNotIn("observations", judgement)

    def test_assurance_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            template = Path(directory) / "default.json"
            result = run(
                "--repo", str(repo), "--write-template", str(template),
            )
            payload = json.loads(template.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["assessment_mode"], "standard")
        self.assertEqual(payload["schema_version"], 5)

    def test_standard_ai_judgements_are_weighted_and_report_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            template = root / "standard.json"
            run(
                "--repo", str(repo), "--mode", "standard",
                "--write-template", str(template),
            )
            assessment = json.loads(template.read_text(encoding="utf-8"))
            assessment["repository_profile"] = {
                "class": "managed",
                "rationale": "AI 根据仓库入口与文档样本完成语义判断。",
                "tags": ["fixture"],
            }
            for dimensions in assessment["dimensions"].values():
                for judgement in dimensions.values():
                    judgement.update({
                        "level": 3,
                        "confidence": "high",
                        "rationale": "当前证据支持成熟度 3，仍需 Owner 验收。",
                        "evidence": [{
                            "kind": "file",
                            "path": "README.md",
                            "locator": "L1",
                            "statement": "仓库入口样本。",
                        }],
                        "limitations": ["只覆盖当前仓库快照。"],
                    })
            template.write_text(
                json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = root / "result.json"
            result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(template), "--format", "json",
                "--output", str(output),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            for dimensions in assessment["dimensions"].values():
                for judgement in dimensions.values():
                    judgement["level"] = 4
            template.write_text(
                json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            high_result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(template), "--format", "json",
                "--output", str(output),
            )
            high_payload = json.loads(output.read_text(encoding="utf-8"))
            first_scope = assessment["dimensions"]["context_control_plane"]
            first_key = next(iter(first_scope))
            for dimensions in assessment["dimensions"].values():
                for judgement in dimensions.values():
                    judgement.update({
                        "level": None,
                        "confidence": "low",
                        "rationale": "",
                        "evidence": [],
                        "limitations": [],
                    })
            first_scope[first_key].update({
                "level": 4,
                "confidence": "high",
                "rationale": "仅完成一个维度。",
                "evidence": [{
                    "kind": "file", "path": "README.md",
                    "locator": "L1", "statement": "仓库入口样本。",
                }],
                "limitations": ["其余维度尚未评审。"],
            })
            template.write_text(
                json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            partial_result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(template), "--format", "json",
                "--output", str(output),
            )
            partial_payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["assessment_mode"], "standard")
        self.assertEqual(payload["score"], 75)
        self.assertEqual(payload["documentation"]["score"], 75)
        self.assertEqual(payload["coverage"]["percent"], 100)
        self.assertEqual(payload["coverage"]["assessed_dimensions"], 14)
        self.assertEqual(payload["confidence_summary"]["high"], 14)
        self.assertEqual(payload["measurement_type"], "agent_assisted_self_assessment")
        self.assertEqual(payload["certification_status"], "not_certified")
        self.assertEqual(high_result.returncode, 0, high_result.stderr)
        self.assertEqual(high_payload["gate_status"], "provisional")
        self.assertEqual(partial_result.returncode, 0, partial_result.stderr)
        self.assertEqual(partial_payload["gate_status"], "not_assessed")
        self.assertLess(partial_payload["coverage"]["percent"], 10)
        self.assertLess(
            partial_payload["score_range"]["min"],
            partial_payload["score_range"]["max"],
        )

    def test_standard_full_dimensions_require_repository_profile_for_provisional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            assessment_path = root / "standard.json"
            run(
                "--repo", str(repo), "--mode", "standard",
                "--write-template", str(assessment_path),
            )
            assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
            for dimensions in assessment["dimensions"].values():
                for judgement in dimensions.values():
                    judgement.update({
                        "level": 4,
                        "confidence": "high",
                        "rationale": "当前证据支持成熟度 4。",
                        "evidence": [{
                            "kind": "file",
                            "path": "README.md",
                            "locator": "L1",
                            "statement": "仓库入口样本。",
                        }],
                        "limitations": ["只覆盖当前仓库快照。"],
                    })
            assessment_path.write_text(
                json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = root / "result.json"
            incomplete_result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(assessment_path), "--format", "json",
                "--output", str(output),
            )
            incomplete_payload = json.loads(output.read_text(encoding="utf-8"))
            assessment["repository_profile"] = {
                "class": "managed",
                "rationale": "AI 根据当前仓库事实完成画像。",
                "tags": [],
            }
            assessment_path.write_text(
                json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            complete_result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(assessment_path), "--format", "json",
                "--output", str(output),
            )
            complete_payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(incomplete_result.returncode, 0, incomplete_result.stderr)
        self.assertEqual(incomplete_payload["score"], 100)
        self.assertEqual(incomplete_payload["documentation"]["score"], 100)
        self.assertEqual(incomplete_payload["coverage"]["assessed_dimensions"], 14)
        self.assertEqual(incomplete_payload["assessment_status"], "partial")
        self.assertEqual(incomplete_payload["gate_status"], "not_assessed")
        self.assertEqual(complete_result.returncode, 0, complete_result.stderr)
        self.assertEqual(complete_payload["assessment_status"], "complete")
        self.assertEqual(complete_payload["gate_status"], "provisional")

    def test_assurance_mode_keeps_observation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            template = Path(directory) / "assurance.json"
            result = run(
                "--repo", str(repo), "--mode", "assurance",
                "--write-template", str(template),
            )
            payload = json.loads(template.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["schema_version"], 4)
        self.assertEqual(payload["product_version"], "1.0")
        self.assertEqual(
            set(payload["assessment_provenance"]),
            {"assessor_id", "run_id", "completed_at"},
        )
        self.assertEqual(
            set(payload["assurance_bindings"]),
            {"standard_assessment", "independent_ai_review"},
        )
        for dimensions in payload["dimensions"].values():
            for judgement in dimensions.values():
                self.assertEqual(len(judgement["observations"]), 4)

    def test_renderer_auto_detects_standard_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            assessment = Path(directory) / "standard.json"
            run(
                "--repo", str(repo), "--mode", "standard",
                "--write-template", str(assessment),
            )
            assessment_payload = json.loads(assessment.read_text(encoding="utf-8"))
            assessment_payload["repository"]["assessment_scope"] = "fixture scope"
            assessment.write_text(
                json.dumps(assessment_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report = Path(directory) / "report.html"
            result = subprocess.run(
                [
                    "python3", str(RENDERER), "--repo", str(repo),
                    "--assessment", str(assessment), "--output", str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            html = report.read_text(encoding="utf-8") if report.exists() else ""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Standard dimension assessment", html)
        self.assertIn("Coverage</dt><dd>0 / 14 (0.0%)", html)
        self.assertIn("Confidence</dt><dd>high 0 · medium 0 · low 0", html)
        self.assertIn("Score range", html)
        self.assertIn("fixture scope", html)
        self.assertIn("权重", html)
        self.assertIn("入口决定所有任务起点", html)
        self.assertIn("证明目标仓库存在可发现", html)
        self.assertIn("不能单凭入口存在", html)
        self.assertNotIn("Validator / Test 回执", html)
        self.assertNotIn("执行指纹", html)

    def test_standard_markdown_contains_full_dimension_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo), "-c", "user.name=Context Fixture",
                    "-c", "user.email=context@example.invalid", "commit", "-qm",
                    "initial context fixture",
                ],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assessment = Path(directory) / "standard.json"
            run("--repo", str(repo), "--write-template", str(assessment))
            payload = json.loads(assessment.read_text(encoding="utf-8"))
            fingerprint = payload["repository"]["worktree_fingerprint"]
            payload["repository"]["assessment_scope"] = "fixture scope"
            assessment.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report = Path(directory) / "standard.md"
            result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(assessment), "--format", "markdown",
                "--output", str(report),
            )
            markdown = report.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Coverage", markdown)
        self.assertIn("Score range", markdown)
        self.assertIn("Confidence", markdown)
        self.assertIn("fixture scope", markdown)
        self.assertIn("Product / schema / mode：`1.0` / `5` / `standard`", markdown)
        self.assertIn(f"HEAD：`{head}`", markdown)
        self.assertIn(f"Worktree fingerprint：`{fingerprint}`", markdown)
        self.assertIn("Git availability：`full`", markdown)
        for key in CONTEXT_KEYS | DOCUMENTATION_KEYS:
            self.assertIn(f"`{key}`", markdown)
        self.assertIn("为什么这样加权", markdown)
        self.assertIn("能证明", markdown)
        self.assertIn("不能证明", markdown)
        self.assertIn("Evidence", markdown)
        self.assertIn("Limitations", markdown)
        self.assertIn("P0 / P1 / P2", markdown)
        self.assertIn("Round 1", markdown)

    def test_malformed_standard_input_returns_structured_invalid_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            assessment = Path(directory) / "standard.json"
            run("--repo", str(repo), "--write-template", str(assessment))
            payload = json.loads(assessment.read_text(encoding="utf-8"))
            del payload["repository_profile"]
            assessment.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = Path(directory) / "result.json"
            result = run(
                "--repo", str(repo), "--mode", "standard",
                "--assessment", str(assessment), "--format", "json",
                "--output", str(output),
            )
            result_payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result_payload["assessment_status"], "invalid")
        self.assertTrue(any(
            "repository_profile" in error
            for error in result_payload["assessment_errors"]
        ))


if __name__ == "__main__":
    unittest.main()
