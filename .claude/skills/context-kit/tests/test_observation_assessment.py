from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_generic_context import (
    build_assessment,
    evidence,
    observation_definitions,
    prepare_layout,
    record_check,
    run_context_kit,
    strong_claim,
    validator_evidence,
    write,
)


def replace_observation(
    payload: dict,
    scope: str,
    dimension: str,
    claim_id: str,
    *,
    status: str,
    fact_path: str,
    evidence_items: list[dict],
) -> None:
    payload["dimensions"][scope][dimension]["observations"][claim_id] = {
        "status": status,
        "rationale": f"Agent 将 {claim_id} 判断为 {status}。",
        "subject": fact_path,
        "evidence": evidence_items,
        "limitations": ["仅覆盖当前测试快照。"],
    }


def init_git(repo: Path) -> str:
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
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def git_item(path: str, commit: str) -> dict:
    return {
        **evidence("git", path, line_start=1, line_end=2),
        "commit": commit,
    }


def owner_review_item(path: str, commit: str, decision: str) -> dict:
    return {
        **evidence("owner_review", path),
        "commit": commit,
        "reviewer": "fixture-owner",
        "decision": decision,
        "reviewed_at": "2026-08-31T00:00:00+00:00",
    }


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def assurance_core_sha256(payload: dict) -> str:
    fields = (
        "product_version", "schema_version", "repository",
        "repository_profile", "assessment_provenance", "dimensions",
        "cross_cutting_findings", "actions",
    )
    return canonical_sha256({field: payload.get(field) for field in fields})


def attach_assurance_bindings(repo: Path, payload: dict) -> tuple[Path, Path]:
    standard_path = repo.parent / "standard-assessment.json"
    subprocess.run(
        [
            "python3", str(Path(__file__).resolve().parents[1] / "scripts/context_kit.py"),
            "--repo", str(repo), "--mode", "standard",
            "--write-template", str(standard_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    standard = json.loads(standard_path.read_text(encoding="utf-8"))
    standard["assessment_provenance"] = {
        "assessor_id": "standard-agent",
        "run_id": "standard-run",
        "completed_at": "2026-09-01T00:00:00+00:00",
    }
    standard["repository_profile"] = {
        "class": "managed",
        "rationale": "AI 根据当前仓库事实完成画像。",
        "tags": [],
    }
    for dimensions in standard["dimensions"].values():
        for judgement in dimensions.values():
            judgement.update({
                "level": 4,
                "confidence": "high",
                "rationale": "当前证据支持成熟度 4。",
                "evidence": [{
                    "kind": "file",
                    "path": "context/entry.md",
                    "locator": "L1-L2",
                    "statement": "仓库 Context 入口样本。",
                }],
                "limitations": ["只覆盖当前仓库快照。"],
            })
    standard_path.write_text(
        json.dumps(standard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    standard_sha = hashlib.sha256(standard_path.read_bytes()).hexdigest()

    payload["assessment_provenance"] = {
        "assessor_id": "assurance-agent",
        "run_id": "assurance-run",
        "completed_at": "2026-09-01T00:10:00+00:00",
    }
    review = {
        "type": "context-kit-independent-ai-review",
        "schema_version": 1,
        "repository": {
            "head": payload["repository"]["head"],
            "worktree_fingerprint": payload["repository"]["worktree_fingerprint"],
        },
        "reviewer": {
            "kind": "ai",
            "reviewer_id": "independent-review-agent",
            "run_id": "review-run",
            "independent_of_run_ids": ["standard-run", "assurance-run"],
        },
        "reviewed_artifacts": {
            "standard_assessment_sha256": standard_sha,
            "assurance_core_sha256": assurance_core_sha256(payload),
        },
        "reviewed_dimensions": {
            "context_control_plane": sorted(
                payload["dimensions"]["context_control_plane"]
            ),
            "repository_documentation": sorted(
                payload["dimensions"]["repository_documentation"]
            ),
        },
        "decision": "approved",
        "findings": [],
        "limitations": ["独立性由不同 run_id 的本地复核记录表达。"],
        "reviewed_at": "2026-09-01T00:20:00+00:00",
    }
    review_path = repo.parent / "independent-ai-review.json"
    review_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["assurance_bindings"] = {
        "standard_assessment": {
            "artifact": str(standard_path),
            "artifact_sha256": standard_sha,
        },
        "independent_ai_review": {
            "artifact": str(review_path),
            "artifact_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        },
    }
    return standard_path, review_path


class ObservationAssessmentIntegrityTests(unittest.TestCase):
    def test_pass_or_partial_file_evidence_requires_line_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["entry_routing"]["observations"].values()
            ))
            observation["evidence"][0].pop("line_end")
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "requires line_start/line_end locator" in error
            for error in assessed["assessment_errors"]
        ))

    def test_strong_receipt_must_bind_same_observation_fact_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            claim_id = strong_claim(
                repo, "context_control_plane", "entry_routing",
            )
            command = ["python3", "checks/context.py", claim_id]
            receipt = record_check(
                repo, claim_id, command, name="unbound-fact",
                expected_output=claim_id,
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload, "context_control_plane", "entry_routing", claim_id,
                status="pass", fact_path="context/entry.md",
                evidence_items=[
                    evidence("file", "context/entry.md", line_start=1, line_end=2),
                    validator_evidence("checks/context.py", command, receipt),
                ],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "must include a file/git evidence path" in error
            for error in assessed["assessment_errors"]
        ))

    def test_grade_a_requires_and_reports_verified_evidence_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            head = init_git(repo)
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="pass",
            )
            first_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["entry_routing"]["observations"].values()
            ))
            first_observation["evidence"].append(git_item("context/entry.md", head))
            authority_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["authority_and_content"]["observations"].values()
            ))
            authority_observation["evidence"].append(
                owner_review_item("context/entry.md", head, "approved")
            )
            attach_assurance_bindings(repo, payload)
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(assessed["evidence_grade"], "A")
        self.assertEqual(assessed["gate_status"], "ready_for_owner_acceptance")
        self.assertIn("Owner/权威复核", assessed["evidence_grade_reason"])
        self.assertIn("独立 AI 复核", assessed["evidence_grade_reason"])
        self.assertTrue(assessed["assurance_bindings"]["ready"])
        self.assertEqual(len(assessed["receipt_evidence"]), 14)
        self.assertEqual(len(assessed["git_assessment_evidence"]), 2)
        receipt = assessed["receipt_evidence"][0]
        self.assertEqual(receipt["observation_id"], receipt["claim"])
        self.assertEqual(receipt["matched_claim_paths"], ["context/entry.md"])
        self.assertEqual(len(receipt["execution_fingerprint"]), 64)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertTrue(receipt["recorded_at"])
        self.assertTrue(receipt["input_hashes"])
        self.assertEqual(len(receipt["stdout_sha256"]), 64)
        self.assertEqual(len(receipt["stderr_sha256"]), 64)
        git_evidence = assessed["git_assessment_evidence"][0]
        self.assertEqual(git_evidence["path"], "context/entry.md")
        self.assertEqual(git_evidence["commit"], head)

    def test_missing_assurance_bindings_cannot_unlock_grade_a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            head = init_git(repo)
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="pass",
            )
            first_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["entry_routing"]["observations"].values()
            ))
            first_observation["evidence"].append(git_item("context/entry.md", head))
            authority_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["authority_and_content"]["observations"].values()
            ))
            authority_observation["evidence"].append(
                owner_review_item("context/entry.md", head, "approved")
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(assessed["evidence_grade"], "B")
        self.assertEqual(assessed["gate_status"], "provisional")
        self.assertFalse(assessed["assurance_bindings"]["ready"])
        self.assertIn("完整 Standard Assessment 绑定", assessed["evidence_grade_reason"])

    def test_changed_assurance_core_invalidates_independent_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            head = init_git(repo)
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="pass",
            )
            first_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["entry_routing"]["observations"].values()
            ))
            first_observation["evidence"].append(git_item("context/entry.md", head))
            authority_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["authority_and_content"]["observations"].values()
            ))
            authority_observation["evidence"].append(
                owner_review_item("context/entry.md", head, "approved")
            )
            attach_assurance_bindings(repo, payload)
            first_observation["rationale"] += " 后续修改。"
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(assessed["evidence_grade"], "B")
        self.assertEqual(assessed["gate_status"], "provisional")
        self.assertFalse(assessed["assurance_bindings"]["ready"])
        self.assertTrue(any(
            "assurance core" in error.lower()
            for error in assessed["assurance_bindings"]["errors"]
        ))

    def test_rejected_owner_review_cannot_unlock_grade_a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            head = init_git(repo)
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="pass",
            )
            first_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["entry_routing"]["observations"].values()
            ))
            first_observation["evidence"].append(git_item("context/entry.md", head))
            authority_observation = next(iter(
                payload["dimensions"]["context_control_plane"]
                ["authority_and_content"]["observations"].values()
            ))
            authority_observation["evidence"].append(
                owner_review_item("context/entry.md", head, "rejected")
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(assessed["evidence_grade"], "B")
        self.assertEqual(assessed["gate_status"], "provisional")
        self.assertIn("已定位且批准的 Owner/权威复核", assessed["evidence_grade_reason"])

    def test_generic_true_and_echo_are_rejected_before_execution(self) -> None:
        for index, command in enumerate((["true"], ["echo", "ok"])):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / f"repository-{index}"
                repo.mkdir()
                prepare_layout(repo, "context/entry.md", "checks/context.py")
                claim_id = strong_claim(
                    repo, "context_control_plane", "entry_routing",
                )
                receipt = repo.parent / f"generic-{index}.json"
                result = subprocess.run(
                    [
                        "python3", str(Path(__file__).resolve().parents[1] / "scripts/context_kit.py"),
                        "--repo", str(repo), "--mode", "assurance",
                        "--record-check", str(receipt),
                        "--claim", claim_id, "--expect", "ok",
                        "--negative-command-json", json.dumps(command),
                        "--negative-input", "checks/context.py.invalid",
                        "--negative-expect", "known-negative",
                        "--", *command,
                    ],
                    check=False, capture_output=True, text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(receipt.exists())
                self.assertIn("unsafe or non-semantic", result.stderr)

    def test_query_shell_and_external_commands_never_execute(self) -> None:
        for index, command_name in enumerate(("grep", "rg", "sh", "curl")):
            with self.subTest(command=command_name), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / f"repository-{index}"
                repo.mkdir()
                prepare_layout(repo, "context/entry.md", "checks/context.py")
                marker = repo / f"{command_name}-executed"
                fake = write(
                    repo, f"bin/{command_name}",
                    f"#!/bin/sh\ntouch '{marker}'\nexit 0\n",
                )
                fake.chmod(0o755)
                claim_id = strong_claim(
                    repo, "context_control_plane", "entry_routing",
                )
                receipt = repo.parent / f"forbidden-{command_name}.json"
                command = [str(fake), "context/entry.md"]
                result = subprocess.run(
                    [
                        "python3", str(Path(__file__).resolve().parents[1] / "scripts/context_kit.py"),
                        "--repo", str(repo), "--mode", "assurance",
                        "--record-check", str(receipt),
                        "--claim", claim_id, "--expect", "ok",
                        "--negative-command-json", json.dumps(command),
                        "--negative-input", "checks/context.py.invalid",
                        "--negative-expect", "known-negative",
                        "--", *command,
                    ],
                    check=False, capture_output=True, text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(receipt.exists())
                self.assertFalse(marker.exists())
                self.assertIn("unsafe or non-semantic", result.stderr)

    def test_negative_fixture_must_be_referenced_by_negative_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            claim_id = strong_claim(repo, "context_control_plane", "entry_routing")
            receipt = repo.parent / "unused-negative-fixture.json"
            positive = ["python3", "checks/context.py", "stable-fragment"]
            negative = ["python3", "checks/context.py", "--negative"]
            result = subprocess.run(
                [
                    "python3", str(Path(__file__).resolve().parents[1] / "scripts/context_kit.py"),
                    "--repo", str(repo), "--mode", "assurance",
                    "--record-check", str(receipt),
                    "--claim", claim_id, "--input", "context/entry.md",
                    "--expect", "stable-fragment",
                    "--negative-command-json", json.dumps(negative),
                    "--negative-input", "checks/context.py.invalid",
                    "--negative-expect", "known-negative",
                    "--", *positive,
                ],
                check=False, capture_output=True, text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(receipt.exists())
            self.assertIn("must be an input referenced", result.stderr)

    def test_receipt_without_claim_binding_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            claim_id = strong_claim(repo, "context_control_plane", "entry_routing")
            command = ["python3", "checks/context.py"]
            receipt = record_check(
                repo, claim_id, command, name="missing-claim",
                expected_output="checks/context.py",
            )
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload.pop("claim_id", None)
            receipt.write_text(
                json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload,
                "context_control_plane",
                "entry_routing",
                claim_id,
                status="pass",
                fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, receipt)],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "claim" in error.lower() for error in assessed["assessment_errors"]
        ))

    def test_claim_bound_receipt_cannot_be_reused_across_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            first_claim = strong_claim(
                repo, "context_control_plane", "entry_routing",
            )
            second_claim = strong_claim(
                repo, "context_control_plane", "routing_coverage",
            )
            command = ["python3", "checks/context.py"]
            receipt = record_check(
                repo, first_claim, command, name="single-claim",
                expected_output="checks/context.py",
            )
            shared_evidence = validator_evidence(
                "checks/context.py", command, receipt,
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload,
                "context_control_plane",
                "entry_routing",
                first_claim,
                status="pass",
                fact_path="context/entry.md",
                evidence_items=[shared_evidence],
            )
            replace_observation(
                payload,
                "context_control_plane",
                "routing_coverage",
                second_claim,
                status="pass",
                fact_path="context/entry.md",
                evidence_items=[shared_evidence],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "claim does not match observation" in error.lower()
            for error in assessed["assessment_errors"]
        ))

    def test_same_check_selector_cannot_be_repackaged_for_two_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            first_claim = strong_claim(
                repo, "context_control_plane", "entry_routing",
            )
            second_claim = strong_claim(
                repo, "context_control_plane", "routing_coverage",
            )
            command = ["python3", "checks/context.py", "shared-selector"]
            first_receipt = record_check(
                repo, first_claim, command, name="selector-first",
                expected_output="shared-selector",
            )
            second_receipt = record_check(
                repo, second_claim, command, name="selector-second",
                expected_output="shared-selector",
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload, "context_control_plane", "entry_routing", first_claim,
                status="pass", fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, first_receipt)],
            )
            replace_observation(
                payload, "context_control_plane", "routing_coverage", second_claim,
                status="pass", fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, second_receipt)],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "reuses the same execution fingerprint" in error.lower()
            for error in assessed["assessment_errors"]
        ))

    def test_changed_output_selector_cannot_hide_same_execution_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            first_claim = strong_claim(repo, "context_control_plane", "entry_routing")
            second_claim = strong_claim(repo, "context_control_plane", "routing_coverage")
            command = ["python3", "checks/context.py", "alpha-beta"]
            first_receipt = record_check(
                repo, first_claim, command, name="expect-alpha", expected_output="alpha",
            )
            second_receipt = record_check(
                repo, second_claim, command, name="expect-beta", expected_output="beta",
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload, "context_control_plane", "entry_routing", first_claim,
                status="pass", fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, first_receipt)],
            )
            replace_observation(
                payload, "context_control_plane", "routing_coverage", second_claim,
                status="pass", fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, second_receipt)],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertTrue(any(
            "reuses the same execution fingerprint" in error.lower()
            for error in assessed["assessment_errors"]
        ))

    def test_unmatched_output_assertion_returns_nonzero_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            claim_id = strong_claim(repo, "context_control_plane", "entry_routing")
            receipt = repo.parent / "unmatched.json"
            result = subprocess.run(
                [
                    "python3", str(Path(__file__).resolve().parents[1] / "scripts/context_kit.py"),
                    "--repo", str(repo), "--mode", "assurance",
                    "--record-check", str(receipt),
                    "--claim", claim_id, "--expect", "not-present",
                    "--negative-command-json", json.dumps([
                        "python3", "checks/context.py", "--negative",
                        "checks/context.py.invalid",
                    ]),
                    "--negative-input", "checks/context.py.invalid",
                    "--negative-expect", "known-negative",
                    "--", "python3", "checks/context.py", "actual-output",
                ],
                check=False, capture_output=True, text=True,
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(payload["meaningful_check"])
        self.assertFalse(payload["output_assertion"]["matched"])

    def test_observation_status_derives_score_and_manual_level_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            derived_payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            derived_result, derived = run_context_kit(repo, derived_payload)
            manual_payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="fail",
            )
            manual_payload["dimensions"]["context_control_plane"]["entry_routing"]["level"] = 4
            manual_result, manual = run_context_kit(repo, manual_payload)

        self.assertEqual(derived_result.returncode, 0, derived_result.stderr)
        self.assertEqual(derived["score"], 50)
        self.assertEqual(derived["documentation"]["score"], 50)
        self.assertEqual(derived["dimensions"]["entry_routing"], 7.5)
        self.assertEqual(derived["derived_levels"]["entry_routing"], 2)
        self.assertEqual(manual_result.returncode, 2)
        self.assertEqual(manual["assessment_status"], "invalid")
        self.assertTrue(any(
            "level is derived" in error.lower()
            for error in manual["assessment_errors"]
        ))

    def test_unknown_observation_withholds_dimension_and_both_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            claim_id = next(iter(
                payload["dimensions"]["context_control_plane"]["entry_routing"]["observations"]
            ))
            payload["dimensions"]["context_control_plane"]["entry_routing"]["observations"][claim_id] = {
                "status": "unknown", "rationale": "", "subject": "",
                "evidence": [], "limitations": [],
            }
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(assessed["assessment_status"], "partial")
        self.assertEqual(assessed["gate_status"], "not_assessed")
        self.assertIsNone(assessed["dimensions"]["entry_routing"])
        self.assertIsNone(assessed["score"])
        self.assertEqual(assessed["documentation"]["score"], 50)

    def test_malformed_receipt_returns_structured_invalid_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            receipt = repo.parent / "malformed-receipt.json"
            receipt.write_text("{not valid json", encoding="utf-8")
            claim_id = strong_claim(repo, "context_control_plane", "entry_routing")
            command = ["python3", "checks/context.py"]
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            replace_observation(
                payload,
                "context_control_plane",
                "entry_routing",
                claim_id,
                status="pass",
                fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, receipt)],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertTrue(any(
            "not valid json" in error.lower()
            for error in assessed["assessment_errors"]
        ))
        self.assertNotIn("Traceback", result.stderr)

    def test_dirty_entry_change_invalidates_receipt_but_not_fresh_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            head = init_git(repo)
            write(
                repo,
                "checks/context.py",
                "# first dirty version\nprint('dirty-ok')\nraise SystemExit(0)\n",
            )
            claim_id = strong_claim(repo, "context_control_plane", "entry_routing")
            command = ["python3", "checks/context.py"]
            receipt = record_check(
                repo, claim_id, command, name="dirty-snapshot",
                expected_output="dirty-ok",
                expect_success=False,
            )
            write(
                repo,
                "checks/context.py",
                "# second dirty version\nprint('dirty-ok')\nraise SystemExit(0)\n",
            )
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            self.assertEqual(payload["repository"]["head"], head)
            replace_observation(
                payload,
                "context_control_plane",
                "entry_routing",
                claim_id,
                status="pass",
                fact_path="context/entry.md",
                evidence_items=[validator_evidence("checks/context.py", command, receipt)],
            )
            result, assessed = run_context_kit(repo, payload)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(assessed["assessment_status"], "invalid")
        self.assertFalse(any(
            "assessment repository worktree fingerprint" in error.lower()
            for error in assessed["assessment_errors"]
        ))
        self.assertTrue(any(
            "receipt dirty worktree snapshot is stale" in error.lower()
            or "receipt input file is stale" in error.lower()
            for error in assessed["assessment_errors"]
        ))

    def test_template_supplies_four_required_observations_per_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repository"
            repo.mkdir()
            prepare_layout(repo, "context/entry.md", "checks/context.py")
            payload, _ = build_assessment(
                repo, "context/entry.md", "checks/context.py", status="partial",
            )
            definitions = observation_definitions(repo)

        for scope, dimensions in payload["dimensions"].items():
            for dimension, entry in dimensions.items():
                expected = {item["id"] for item in definitions[scope][dimension]}
                self.assertEqual(set(entry["observations"]), expected)
                self.assertEqual(len(entry["observations"]), 4)


if __name__ == "__main__":
    unittest.main()
