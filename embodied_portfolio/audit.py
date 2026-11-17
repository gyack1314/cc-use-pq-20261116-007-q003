from __future__ import annotations

from decimal import Decimal
from typing import Any
import json

from .contracts import Offer, Requirement, TransferEvidence, PortfolioError, canonical
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger, digest
from .optimizer import PortfolioOptimizer


class DecisionAudit:
    """仅使用冻结输入重算历史决策，不依赖当前报价清单或当前证据状态。"""

    def __init__(self, ledger: Ledger, coverage: CoverageEngine, prices: PriceEngine,
                 optimizer: PortfolioOptimizer):
        self.ledger = ledger
        self.coverage = coverage
        self.prices = prices
        self.optimizer = optimizer

    @staticmethod
    def _jsonify(value: Any) -> Any:
        """把元组等内存表示统一成与落盘 JSON 相同的形状。"""
        return json.loads(canonical(value))

    @staticmethod
    def _contains(stored: Any, recomputed: Any) -> bool:
        """冻结报告是当前重算结果的子集时视为一致。

        这样引擎在新增迁移字段后，升级前已批准的结论仍可按当时依据复核；
        报告被篡改（多出未知字段、缺字段或值不同）仍会暴露。
        """
        stored = DecisionAudit._jsonify(stored)
        recomputed = DecisionAudit._jsonify(recomputed)
        if isinstance(stored, dict):
            return isinstance(recomputed, dict) and all(
                name in recomputed and DecisionAudit._contains(value, recomputed[name])
                for name, value in stored.items())
        if isinstance(stored, list):
            return (isinstance(recomputed, list) and len(stored) == len(recomputed)
                    and all(DecisionAudit._contains(left, right)
                            for left, right in zip(stored, recomputed)))
        return stored == recomputed

    def _inputs(self, analysis_id: int) -> tuple[dict[str, Any], Requirement,
                                                 list[dict[str, Any]], list[Offer],
                                                 list[dict[str, Any]], list[TransferEvidence]]:
        documents = self.ledger.analysis_inputs(analysis_id)
        requirements = [doc for doc in documents if doc["kind"] == "requirement"]
        if len(requirements) != 1:
            raise PortfolioError("snapshot_inputs", "analysis needs one requirement revision")
        offer_docs = [doc for doc in documents if doc["kind"] == "offer"]
        evidence_docs = [doc for doc in documents if doc["kind"] == "evidence"]
        requirement = Requirement.parse(requirements[0]["body"])
        evidence = [TransferEvidence.parse(doc["body"]) for doc in evidence_docs]
        if requirement.transfer_policy == "exact":
            evidence = []
        return (requirements[0], requirement, offer_docs,
                [Offer.parse(doc["body"]) for doc in offer_docs], evidence_docs, evidence)

    def explain(self, analysis_id: int) -> dict[str, Any]:
        analysis = self.ledger.analysis(analysis_id)
        requirement_doc, requirement, offer_docs, offers, evidence_docs, evidence = \
            self._inputs(analysis_id)
        stored = analysis["report"]
        snapshot = self.ledger.verify(analysis_id)
        all_docs = [requirement_doc, *offer_docs, *evidence_docs]
        source_integrity = all(digest(doc["body"]) == doc["digest"] for doc in all_docs)
        report_selection = stored.get("selection", [])
        selected = [offer for offer in offers if offer.package in report_selection]
        unknown = sorted(set(report_selection) - {offer.package for offer in offers})
        historical = self.optimizer.search(requirement, offers, evidence)
        recomputed = list(historical.best.packages) if historical.best else []
        selected_coverage = self.coverage.evaluate(requirement, selected, evidence)
        quotes = self.prices.all_quotes(selected, requirement)
        recomputed_cost = self.prices.total(quotes) if historical.best else None
        candidate = historical.candidate_best if not historical.best else None
        candidate_offers = ([offer for offer in offers if offer.package in candidate.packages]
                            if candidate else [])
        candidate_coverage = (self.coverage.evaluate(requirement, candidate_offers, evidence)
                              if candidate else None)
        candidate_quotes = self.prices.all_quotes(candidate_offers, requirement) if candidate else []
        mismatches = []
        if not snapshot["verified"]:
            mismatches.append("snapshot_digest")
        if not source_integrity:
            mismatches.append("source_digest")
        if unknown:
            mismatches.append("unknown_selected_package")
        if report_selection != recomputed:
            mismatches.append("selection")
        if stored.get("feasible") != (historical.best is not None):
            mismatches.append("feasibility")
        if stored.get("cost") != recomputed_cost:
            mismatches.append("cost")
        if not self._contains(stored.get("coverage"), selected_coverage.document()):
            mismatches.append("coverage")
        if stored.get("schema_version", 1) >= 2 and not self._contains(
                stored.get("verified_coverage"), selected_coverage.document()):
            mismatches.append("verified_coverage")
        if stored.get("quotes") != [quote.document() for quote in quotes]:
            mismatches.append("quotes")
        if not self._contains(stored.get("search"), historical.document()):
            mismatches.append("search")
        if stored.get("provider_spend") != self.prices.provider_breakdown(quotes):
            mismatches.append("provider_spend")
        if not self._contains(stored.get("selected_attribution"),
                              self.coverage.attribution(requirement, selected, evidence)):
            mismatches.append("attribution")
        if stored.get("cost_per_credited_sample") != (
                self.prices.cost_per_credited_sample(recomputed_cost, selected_coverage.total_credited)
                if recomputed_cost is not None else None):
            mismatches.append("unit_cost")
        stored_candidate = stored.get("candidate") or {}
        expected_candidate = {
            "selection": list(candidate.packages) if candidate else [],
            "cost": candidate.cost if candidate else None,
            "coverage": candidate_coverage.document() if candidate_coverage else None,
        }
        if stored.get("schema_version", 1) >= 2:
            if not self._contains(stored_candidate.get("coverage"), expected_candidate["coverage"]):
                mismatches.append("candidate_coverage")
            if stored_candidate.get("selection") != expected_candidate["selection"]:
                mismatches.append("candidate_selection")
            if stored_candidate.get("cost") != expected_candidate["cost"]:
                mismatches.append("candidate_cost")
        evidence_lineage = [{"kind": "evidence", "key": doc["key"],
                             "revision": doc["revision"], "digest": doc["digest"],
                             "status": doc["body"].get("status")}
                            for doc in evidence_docs]
        lineage = [{"kind": doc["kind"], "key": doc["key"],
                    "revision": doc["revision"], "digest": doc["digest"]}
                   for doc in [requirement_doc, *offer_docs]]
        current = self.ledger.latest_input_digest(requirement.key)
        return {"analysis_id": analysis_id, "state": analysis["state"],
                "historical_valid": not mismatches, "mismatches": mismatches,
                "input_current": current == analysis["input_digest"],
                "transfer_policy": requirement.transfer_policy,
                "lineage": [*lineage, *evidence_lineage],
                "evidence_versions": evidence_lineage,
                "evidence_state_current": self._evidence_current(evidence_docs, requirement),
                "selection": recomputed,
                "cost": recomputed_cost, "coverage": selected_coverage.document(),
                "candidate": {"selection": expected_candidate["selection"],
                              "cost": expected_candidate["cost"],
                              "coverage": expected_candidate["coverage"],
                              "quotes": [quote.document() for quote in candidate_quotes]},
                "snapshot_digest_valid": snapshot["verified"],
                "source_digest_valid": source_integrity}

    def _evidence_current(self, evidence_docs: list[dict[str, Any]],
                          requirement: Requirement) -> dict[str, Any] | None:
        """证据在分析后被撤回或修订时为 False；历史结论本身不受影响。"""
        if requirement.transfer_policy == "exact":
            return None
        current = {doc["key"]: doc for doc in self.ledger.latest_evidence()}
        stale = []
        for doc in evidence_docs:
            latest = current.get(doc["key"])
            if latest is None or latest["id"] != doc["id"]:
                stale.append({"key": doc["key"], "snapshot_revision": doc["revision"],
                              "current_revision": latest["revision"] if latest else None,
                              "snapshot_status": doc["body"].get("status"),
                              "current_status": latest["body"].get("status") if latest else None})
        return {"all_current": not stale, "changed": stale}

    def compare(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        if earlier_id == later_id:
            raise PortfolioError("analysis_pair", "choose two distinct analyses")
        earlier = self.ledger.analysis(earlier_id)
        later = self.ledger.analysis(later_id)
        if earlier["requirement_key"] != later["requirement_key"]:
            raise PortfolioError("requirement_pair", "analyses must share requirement key")
        first = self.explain(earlier_id)
        second = self.explain(later_id)
        if not first["historical_valid"] or not second["historical_valid"]:
            raise PortfolioError("invalid_snapshot", "cannot compare invalid analysis")
        a = {doc["key"]: doc for doc in first["lineage"] if doc["kind"] == "offer"}
        b = {doc["key"]: doc for doc in second["lineage"] if doc["kind"] == "offer"}
        a_selected = set(first["selection"])
        b_selected = set(second["selection"])
        changes = []
        for package in sorted(a.keys() | b.keys()):
            left = a.get(package)
            right = b.get(package)
            if left is None or right is None or left["digest"] != right["digest"]:
                changes.append({"package": package,
                                "earlier_revision": left["revision"] if left else None,
                                "later_revision": right["revision"] if right else None})
        evidence_changes = []
        left_evidence = {doc["key"]: doc for doc in first["evidence_versions"]}
        right_evidence = {doc["key"]: doc for doc in second["evidence_versions"]}
        for key in sorted(left_evidence.keys() | right_evidence.keys()):
            left = left_evidence.get(key)
            right = right_evidence.get(key)
            if left is None or right is None or left["digest"] != right["digest"]:
                evidence_changes.append({"evidence": key,
                                         "earlier_revision": left["revision"] if left else None,
                                         "later_revision": right["revision"] if right else None,
                                         "earlier_status": left["status"] if left else None,
                                         "later_status": right["status"] if right else None})
        left_cost = earlier["report"].get("cost")
        right_cost = later["report"].get("cost")
        cost_delta = str(Decimal(right_cost) - Decimal(left_cost)) if left_cost and right_cost else None
        left_candidate = (earlier["report"].get("candidate") or {}).get("selection") or []
        right_candidate = (later["report"].get("candidate") or {}).get("selection") or []
        a_cells = {cell["slice"]: cell for cell in first["coverage"]["cells"]}
        b_cells = {cell["slice"]: cell for cell in second["coverage"]["cells"]}
        coverage_changes = [{"slice": token,
                             "credited_before": a_cells.get(token, {}).get("credited", 0),
                             "credited_after": b_cells.get(token, {}).get("credited", 0),
                             "candidate_credited_before": a_cells.get(token, {}).get(
                                 "candidate_credited", 0),
                             "candidate_credited_after": b_cells.get(token, {}).get(
                                 "candidate_credited", 0),
                             "shortage_before": a_cells.get(token, {}).get("shortage", 0),
                             "shortage_after": b_cells.get(token, {}).get("shortage", 0)}
                            for token in sorted(a_cells.keys() | b_cells.keys())
                            if a_cells.get(token) != b_cells.get(token)]
        return {"requirement": earlier["requirement_key"], "earlier": earlier_id,
                "later": later_id, "requirement_revision_changed":
                earlier["requirement_revision"] != later["requirement_revision"],
                "offer_revisions_changed": changes,
                "evidence_revisions_changed": evidence_changes,
                "packages_added": sorted(b_selected - a_selected),
                "packages_removed": sorted(a_selected - b_selected),
                "candidate_packages_before": sorted(left_candidate),
                "candidate_packages_after": sorted(right_candidate),
                "cost_delta": cost_delta, "coverage_changes": coverage_changes}
