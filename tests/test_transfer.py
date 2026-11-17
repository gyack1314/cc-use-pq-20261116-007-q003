from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from embodied_portfolio.cli import transfer_demo_payloads
from embodied_portfolio.contracts import Offer, Requirement, TransferEvidence, PortfolioError
from embodied_portfolio.coverage import CoverageEngine
from embodied_portfolio.decision import PortfolioService
from embodied_portfolio.diagnostics import SupplyDiagnostics
from embodied_portfolio.economics import PriceEngine
from embodied_portfolio.ledger import digest
from embodied_portfolio.optimizer import PortfolioOptimizer


def evidence_for(package="portable_transfer", source="arm_b", target="arm_a",
                 stage="place", status="validated", key="transfer_place_v1"):
    return {"key": key, "package": package, "source_robot": source, "target_robot": target,
            "scopes": [{"scenario": "warehouse_pick", "stage": stage, "outcome": "failure"}],
            "status": status, "reference": "bench-report", "validity": "2026"}


class TransferCoverageTests(unittest.TestCase):
    def setUp(self):
        requirement, offers, evidence = transfer_demo_payloads()
        self.requirement = Requirement.parse(requirement)
        self.offers = [Offer.parse(item) for item in offers]
        self.evidence = [TransferEvidence.parse(item) for item in evidence]
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.diagnostics = SupplyDiagnostics(self.coverage, self.prices)
        self.grasp = "warehouse_pick|grasp|failure|arm_a"
        self.place = "warehouse_pick|place|failure|arm_a"

    def cells(self, result):
        return {cell.slice: cell for cell in result.cells}

    def test_validated_evidence_is_credited_and_pending_is_candidate(self):
        result = self.coverage.evaluate(self.requirement, self.offers, self.evidence)
        cells = self.cells(result)
        self.assertFalse(result.mandatory_complete)
        self.assertTrue(result.candidate_mandatory_complete)
        self.assertEqual(cells[self.grasp].credited, 4)
        self.assertEqual(cells[self.grasp].status, "covered")
        self.assertEqual(cells[self.grasp].transferred_available, 4)
        self.assertEqual(cells[self.place].credited, 0)
        self.assertEqual(cells[self.place].candidate_credited, 2)
        self.assertEqual(cells[self.place].candidate_shortage, 0)
        self.assertEqual(cells[self.place].status, "candidate_only")
        self.assertEqual(cells[self.place].candidate_evidence_keys, ("transfer_place_v1",))
        self.assertEqual(result.total_candidate_credited, 2)

    def test_pending_evidence_never_counts_as_verified(self):
        pending = [TransferEvidence.parse(evidence_for(stage="grasp", status="pending"))]
        result = self.coverage.evaluate(self.requirement, self.offers, pending)
        cells = self.cells(result)
        # 只有 teleop_direct 的 2 条直接样本进入已验证覆盖，4 条便携样本只是候选。
        self.assertEqual(cells[self.grasp].credited, 2)
        self.assertEqual(cells[self.grasp].shortage, 2)
        self.assertEqual(cells[self.grasp].candidate_credited, 2)
        self.assertEqual(cells[self.grasp].candidate_shortage, 0)
        # place 没有任何证据，候选供给也无法补足。
        self.assertEqual(cells[self.place].candidate_shortage, 2)
        self.assertFalse(result.mandatory_complete)
        self.assertFalse(result.candidate_mandatory_complete)

    def test_retracted_evidence_is_ignored_entirely(self):
        retracted = [TransferEvidence.parse(evidence_for(stage="grasp", status="retracted"))]
        result = self.coverage.evaluate(self.requirement, self.offers, retracted)
        cells = self.cells(result)
        self.assertEqual(cells[self.grasp].available, 2)
        self.assertEqual(cells[self.grasp].candidate_available, 0)
        self.assertFalse(result.mandatory_complete)

    def test_evidence_for_other_target_robot_does_not_apply(self):
        wrong = [TransferEvidence.parse(evidence_for(stage="grasp", target="arm_c"))]
        result = self.coverage.evaluate(self.requirement, self.offers, wrong)
        self.assertEqual(result.total_credited, 2)

    def test_evidence_source_must_match_observation_robot(self):
        wrong = [TransferEvidence.parse(evidence_for(stage="grasp", source="arm_c"))]
        result = self.coverage.evaluate(self.requirement, self.offers, wrong)
        self.assertEqual(result.total_credited, 2)

    def test_exact_policy_ignores_evidence(self):
        exact = Requirement.parse({**self.requirement.document(), "transfer_policy": "exact"})
        result = self.coverage.evaluate(exact, self.offers, self.evidence)
        cells = self.cells(result)
        self.assertEqual(result.total_credited, 2)
        self.assertEqual(cells[self.grasp].candidate_available, 0)
        self.assertFalse(result.mandatory_complete)

    def test_requirement_defaults_to_exact_policy(self):
        body = self.requirement.document()
        body.pop("transfer_policy")
        self.assertEqual(Requirement.parse(body).transfer_policy, "exact")

    def test_evidence_contract_rejects_same_robot(self):
        with self.assertRaises(PortfolioError) as caught:
            TransferEvidence.parse({**evidence_for(), "target_robot": "arm_b"})
        self.assertEqual(caught.exception.code, "transfer_robots")

    def test_evidence_status_aliases_are_normalized(self):
        item = TransferEvidence.parse({**evidence_for(), "status": "withdrawn"})
        self.assertEqual(item.status, "retracted")
        self.assertEqual(TransferEvidence.parse({**evidence_for(), "status": "verified"}).status,
                         "validated")
        with self.assertRaises(PortfolioError) as caught:
            TransferEvidence.parse({**evidence_for(), "status": "maybe"})
        self.assertEqual(caught.exception.code, "evidence_status")

    def test_evidence_requires_declared_scope(self):
        body = evidence_for()
        body.pop("scopes")
        with self.assertRaises(PortfolioError) as caught:
            TransferEvidence.parse(body)
        self.assertEqual(caught.exception.code, "evidence_scope")

    def test_verified_and_candidate_are_reported_separately(self):
        report = self.diagnostics.data_readiness(self.requirement, self.offers, self.evidence)
        self.assertFalse(report["mandatory_possible"])
        self.assertTrue(report["mandatory_possible_with_candidates"])
        self.assertEqual(report["target_shortage"], 2)
        self.assertEqual(report["candidate_shortage"], 0)
        coverage = {row["slice"]: row for row in report["evidence_coverage"]}
        self.assertEqual(coverage[self.grasp]["validated_transfer_count"], 4)
        self.assertEqual(coverage[self.place]["validated_transfer_count"], 0)
        self.assertEqual(coverage[self.place]["pending_transfer_count"], 2)

    def test_brief_marks_evidence_usable_and_pending(self):
        pairs = [(doc_document(item), TransferEvidence.parse(item)) for item in
                 transfer_demo_payloads()[2]]
        report = self.diagnostics.procurement_brief(self.requirement, self.offers, pairs)
        statuses = {row["key"]: row["status"] for row in report["evidence"]["evidence"]}
        self.assertEqual(statuses, {"transfer_grasp_v1": "validated",
                                    "transfer_place_v1": "pending"})
        self.assertEqual(report["evidence"]["status_counts"]["validated"], 1)
        place = next(row for row in report["target_supply"] if row["slice"] == self.place)
        self.assertEqual(place["shortage"], 2)
        self.assertEqual(place["candidate_shortage"], 0)
        self.assertEqual(place["candidate_available"], 2)
        contributor = place["contributors"][0]
        self.assertEqual(contributor["pending_transfer_count"], 2)
        self.assertEqual(contributor["validated_transfer_count"], 0)

    def test_optimizer_keeps_unverified_only_portfolio_out_of_feasible(self):
        result = self.optimizer.search(self.requirement, self.offers, self.evidence)
        self.assertIsNone(result.best)
        self.assertEqual(result.reason, "mandatory_targets_require_unverified_evidence")
        self.assertIsNotNone(result.candidate_best)
        self.assertEqual(result.candidate_best.packages, ("portable_transfer",))
        self.assertEqual(result.candidate_best.credited, 4)
        self.assertEqual(result.candidate_best.candidate_credited, 2)

    def test_optimizer_can_select_when_all_mandatory_verified(self):
        validated_place = [TransferEvidence.parse(evidence_for(stage="place", status="validated",
                                                             key="transfer_place_v2"))]
        items = [*self.evidence, *validated_place]
        result = self.optimizer.search(self.requirement, self.offers, items)
        self.assertIsNotNone(result.best)
        self.assertEqual(result.best.packages, ("portable_transfer",))
        self.assertEqual(result.best.shortage, 0)

    def test_optimizer_requires_verified_portfolio_even_if_cheaper_candidate_exists(self):
        budget = Requirement.parse({**self.requirement.document(), "budget": "100"})
        result = self.optimizer.search(budget, self.offers, self.evidence)
        self.assertIsNone(result.best)
        self.assertEqual(result.candidate_best.packages, ("portable_transfer",))
        self.assertEqual(result.candidate_best.cost, "60.00")

    def test_marginal_separates_verified_and_candidate_gain(self):
        pending = [TransferEvidence.parse(evidence_for(stage="place", status="pending"))]
        report = self.coverage.marginal(self.requirement, [], self.offers[1], pending)
        # 便携包单独加入时没有抓取阶段的已验证证据，只有 place 的待确认供给。
        self.assertEqual(report["credited_gain"], 0)
        self.assertEqual(report["candidate_credited_gain"], 2)
        self.assertEqual(report["changes"][1]["remaining_candidate_shortage"], 0)

    def test_gap_report_exposes_candidate_only_slice(self):
        report = self.coverage.gap_report(self.requirement, self.offers, self.evidence)
        place = next(row for row in report["gaps"] if row["slice"] == self.place)
        self.assertEqual(place["missing"], 2)
        self.assertEqual(place["candidate_missing"], 0)
        self.assertEqual(place["candidate_providers"], ["portable_transfer"])
        self.assertEqual(place["status"], "candidate_only")


def doc_document(value):
    return {"revision": 1, "digest": "x", "body": value, "key": value["key"]}


class TransferDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "transfer.db"
        self.service = PortfolioService(self.path)
        self.addCleanup(self.service.close)
        self.requirement, self.offers, self.evidence = transfer_demo_payloads()
        self.service.import_requirement(self.requirement)
        for offer in self.offers:
            self.service.import_offer(offer)
        for item in self.evidence:
            self.service.import_evidence(item)

    def validated_place(self):
        return {"key": "transfer_place_v2", "package": "portable_transfer",
                "source_robot": "arm_b", "target_robot": "arm_a",
                "scopes": [{"scenario": "warehouse_pick", "stage": "place", "outcome": "failure"}],
                "status": "validated", "reference": "bench-report-2026-02", "validity": "2026"}

    def test_plan_separates_verified_and_candidate(self):
        report = self.service.plan("transfer_trial")["report"]
        self.assertFalse(report["feasible"])
        self.assertEqual(report["selection"], [])
        self.assertIsNone(report["cost"])
        self.assertEqual(report["candidate"]["selection"], ["portable_transfer"])
        self.assertEqual(report["candidate"]["cost"], "60.00")
        candidate_cells = {cell["slice"]: cell for cell
                           in report["candidate"]["coverage"]["cells"]}
        self.assertEqual(candidate_cells["warehouse_pick|place|failure|arm_a"]
                         ["candidate_credited"], 2)
        self.assertEqual(report["candidate"]["coverage"]["total_candidate_credited"], 2)
        self.assertEqual([item["key"] for item in report["evidence_versions"]],
                         ["transfer_grasp_v1", "transfer_place_v1"])

    def test_approval_requires_verified_coverage(self):
        analysis = self.service.plan("transfer_trial")
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "infeasible")

    def test_validated_evidence_revision_enables_verified_selection(self):
        self.service.import_evidence(self.validated_place())
        report = self.service.plan("transfer_trial")["report"]
        self.assertTrue(report["feasible"])
        self.assertEqual(report["selection"], ["portable_transfer"])
        self.assertEqual(report["cost"], "60.00")
        self.assertEqual(report["coverage"]["total_shortage"], 0)
        self.assertEqual(report["coverage"]["total_candidate_shortage"], 0)

    def test_evidence_revision_invalidates_pending_approval(self):
        self.service.import_evidence(self.validated_place())
        analysis = self.service.plan("transfer_trial")
        approved = self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(approved["state"], "approved")
        revised = {**self.evidence[0], "reference": "bench-report-2026-03"}
        self.service.import_evidence(revised)
        stale = self.service.stale_drafts("transfer_trial")
        self.assertEqual(stale, [])
        audit = self.service.audit_analysis(analysis["id"])
        self.assertTrue(audit["historical_valid"])
        self.assertFalse(audit["evidence_state_current"]["all_current"])
        self.assertEqual(audit["evidence_state_current"]["changed"][0]["key"],
                         "transfer_grasp_v1")

    def test_withdrawn_evidence_invalidates_unapproved_analysis(self):
        self.service.import_evidence(self.validated_place())
        analysis = self.service.plan("transfer_trial")
        self.assertTrue(analysis["report"]["feasible"])
        withdrawn = {**self.validated_place(), "status": "withdrawn"}
        self.service.import_evidence(withdrawn)
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "stale_input")
        pending = self.service.stale_drafts("transfer_trial")
        self.assertEqual(pending[0]["analysis_id"], analysis["id"])
        replanned = self.service.plan("transfer_trial")
        self.assertFalse(replanned["report"]["feasible"])
        self.assertEqual(replanned["report"]["candidate"]["selection"], ["portable_transfer"])

    def test_replan_recomputes_unapproved_analysis_after_retraction(self):
        self.service.import_evidence(self.validated_place())
        analysis = self.service.plan("transfer_trial")
        self.assertTrue(analysis["report"]["feasible"])
        self.service.import_evidence({**self.validated_place(), "status": "retracted"})
        superseded = self.service.replan(analysis["id"])
        self.assertEqual(superseded["supersedes"], analysis["id"])
        self.assertFalse(superseded["report"]["feasible"])
        self.assertEqual(superseded["report"]["candidate"]["selection"], ["portable_transfer"])
        self.assertEqual(self.service.analysis(analysis["id"])["report"]["feasible"], True)

    def test_approved_analysis_cannot_be_replanned(self):
        self.service.import_evidence(self.validated_place())
        analysis = self.service.plan("transfer_trial")
        self.service.approve(analysis["id"], analysis["input_digest"])
        with self.assertRaises(PortfolioError) as caught:
            self.service.replan(analysis["id"])
        self.assertEqual(caught.exception.code, "approved_immutable")
        self.assertTrue(self.service.verify(analysis["id"])["verified"])

    def test_approved_snapshot_survives_evidence_retraction(self):
        self.service.import_evidence(self.validated_place())
        analysis = self.service.plan("transfer_trial")
        self.service.approve(analysis["id"], analysis["input_digest"])
        self.service.import_evidence({**self.validated_place(), "status": "retracted"})
        self.assertTrue(self.service.verify(analysis["id"])["verified"])
        audit = self.service.audit_analysis(analysis["id"])
        self.assertTrue(audit["historical_valid"])
        self.assertEqual(audit["cost"], "60.00")
        self.assertEqual(audit["selection"], ["portable_transfer"])
        self.assertEqual({item["key"]: item["status"] for item in audit["evidence_versions"]},
                         {"transfer_grasp_v1": "validated",
                          "transfer_place_v1": "pending",
                          "transfer_place_v2": "validated"})
        self.assertEqual(self.service.analysis(analysis["id"])["report"]["cost"], "60.00")

    def test_price_scenario_is_read_only_for_evidence(self):
        before = self.service.status()
        rows = self.service.budget_grid("transfer_trial", ["60", "220"])
        self.assertEqual(before["evidence_versions"], 2)
        self.assertEqual(self.service.status()["evidence_versions"], 2)
        self.assertEqual(rows[0]["candidate_selected"], ["portable_transfer"])

    def test_evidence_withdrawal_scenario_is_read_only(self):
        result = self.service.evidence_withdrawal("transfer_trial", "transfer_grasp_v1")
        self.assertFalse(result["after_feasible"])
        self.assertEqual(self.service.document_history("evidence", "transfer_grasp_v1")[-1]["revision"], 1)

    def test_compare_analyses_reports_evidence_revision(self):
        first = self.service.plan("transfer_trial")
        self.service.import_evidence(self.validated_place())
        second = self.service.plan("transfer_trial")
        comparison = self.service.compare_analyses(first["id"], second["id"])
        self.assertEqual(comparison["evidence_revisions_changed"][0]["evidence"], "transfer_place_v2")
        self.assertEqual(comparison["packages_added"], ["portable_transfer"])
        self.assertEqual(comparison["candidate_packages_before"], ["portable_transfer"])

    def test_evidence_version_history_and_diff(self):
        self.service.import_evidence({**self.validated_place(), "status": "pending"})
        self.service.import_evidence(self.validated_place())
        history = self.service.document_history("evidence", "transfer_place_v2")
        self.assertEqual([doc["revision"] for doc in history], [1, 2])
        comparison = self.service.compare_versions("evidence", "transfer_place_v2", 1, 2)
        self.assertEqual(comparison["changes"][0]["field"], "status")
        self.assertEqual(comparison["changes"][0]["earlier"], "pending")
        self.assertEqual(comparison["changes"][0]["later"], "validated")

    def test_batch_import_accepts_evidence(self):
        batch = {"documents": [{"kind": "evidence", "body": self.validated_place()}]}
        result = self.service.catalog.import_batch(batch)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.service.status()["evidence_count"], 3)

    def test_evidence_for_unknown_package_is_flagged_in_inventory(self):
        self.service.import_evidence({**self.validated_place(), "package": "missing_box"})
        brief = self.service.brief("transfer_trial")
        row = next(item for item in brief["evidence"]["evidence"]
                   if item["key"] == "transfer_place_v2")
        self.assertFalse(row["usable"])
        self.assertIn("unknown_package", row["issues"])

    def test_evidence_from_undeclared_source_robot_is_flagged(self):
        self.service.import_evidence({**self.validated_place(), "source_robot": "arm_c"})
        brief = self.service.brief("transfer_trial")
        row = next(item for item in brief["evidence"]["evidence"]
                   if item["key"] == "transfer_place_v2")
        self.assertFalse(row["usable"])
        self.assertIn("source_robot_not_declared", row["issues"])
        self.assertFalse(self.service.plan("transfer_trial")["report"]["feasible"])

    def test_exact_policy_plan_ignores_evidence_and_approves_by_exact_robots(self):
        direct = {**self.requirement, "key": "exact_trial", "transfer_policy": "exact",
                  "targets": [self.requirement["targets"][0]]}
        self.service.import_requirement(direct)
        report = self.service.plan("exact_trial")["report"]
        self.assertFalse(report["feasible"])
        self.assertEqual(report["selection"], [])
        self.assertEqual(report["evidence_versions"], [])
        self.assertEqual(report["coverage"]["cells"][0]["transferred_available"], 0)

    def test_transfer_demo_is_usable_from_cli(self):
        from embodied_portfolio.cli import transfer_demo
        result = transfer_demo(str(Path(self.tmp.name) / "demo"))
        self.assertFalse(result["feasible"])
        self.assertEqual(result["candidate_selection"], ["portable_transfer"])
        self.assertEqual(result["candidate_cost"], "60.00")
        self.assertEqual(result["verified_credited"], 0)
        self.assertEqual(result["candidate_credited_with_pending"], 2)

    def test_old_snapshot_without_new_fields_still_audits(self):
        """升级前写入的报告缺少迁移字段时，仍能按当时依据复核。"""
        analysis = self.service.plan("transfer_trial")
        stored = self.service.analysis(analysis["id"])["report"]
        stripped = {key: value for key, value in stored.items()
                    if key not in {"candidate", "verified_coverage", "evidence",
                                   "evidence_coverage", "evidence_versions", "transfer_policy",
                                   "target_supply"}}
        stripped["schema_version"] = 1
        for cell in stripped["coverage"]["cells"]:
            for field in ("candidate_available", "candidate_credited", "candidate_shortage",
                          "direct_available", "transferred_available", "candidate_packages",
                          "direct_packages", "transferred_packages", "evidence_keys",
                          "candidate_evidence_keys", "status"):
                cell.pop(field, None)
        stripped["search"].pop("candidate_best", None)
        stripped["search"].pop("candidate_alternatives", None)
        stripped["search"].pop("best_status", None)
        self.service.ledger.db.execute("UPDATE analyses SET report=?, report_digest=? WHERE id=?",
                                       (json.dumps(stripped, ensure_ascii=False, sort_keys=True),
                                        digest(stripped), analysis["id"]))
        audit = self.service.audit_analysis(analysis["id"])
        self.assertTrue(audit["historical_valid"], audit["mismatches"])


if __name__ == "__main__":
    unittest.main()
