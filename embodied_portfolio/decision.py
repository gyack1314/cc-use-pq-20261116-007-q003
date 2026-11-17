from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import Catalog
from .contracts import Requirement, PortfolioError
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger
from .optimizer import PortfolioOptimizer
from .sensitivity import SensitivityAnalyzer
from .diagnostics import SupplyDiagnostics
from .audit import DecisionAudit


class PortfolioService:
    def __init__(self, db_path: str | Path):
        self.ledger = Ledger(db_path)
        self.catalog = Catalog(self.ledger)
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.sensitivity = SensitivityAnalyzer(self.optimizer, self.coverage, self.prices)
        self.diagnostics = SupplyDiagnostics(self.coverage, self.prices)
        self.audit = DecisionAudit(self.ledger, self.coverage, self.prices, self.optimizer)

    def close(self):
        self.ledger.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def import_offer(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_offer(body)

    def import_requirement(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_requirement(body)

    def import_evidence(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_evidence(body)

    def import_file(self, kind: str, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_file(kind, path)

    def import_batch_file(self, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_batch_file(path)

    def _evidence(self, requirement: Requirement):
        if requirement.transfer_policy == "exact":
            return [], []
        pairs = self.catalog.evidence()
        return [item for _, item in pairs], [doc for doc, _ in pairs]

    def plan(self, requirement_key: str) -> dict[str, Any]:
        requirement_doc, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        offers = [offer for _, offer in offer_pairs]
        evidence, evidence_docs = self._evidence(requirement)
        search = self.optimizer.search(requirement, offers, evidence)
        selected = [offer for offer in offers if search.best and offer.package in search.best.packages]
        candidate = (search.candidate_best if not search.best else None)
        selected_quotes = self.prices.all_quotes(selected, requirement)
        selected_coverage = self.coverage.evaluate(requirement, selected, evidence)
        candidate_offers = ([offer for offer in offers if offer.package in candidate.packages]
                            if candidate else [])
        candidate_coverage = (self.coverage.evaluate(requirement, candidate_offers, evidence)
                              if candidate else None)
        candidate_quotes = self.prices.all_quotes(candidate_offers, requirement) if candidate else []
        report = {"schema_version": 2, "requirement": requirement.key,
                  "requirement_revision": requirement_doc["revision"],
                  "client": requirement.client, "budget": requirement.budget,
                  "transfer_policy": requirement.transfer_policy,
                  "feasible": search.best is not None,
                  "selection": list(search.best.packages) if search.best else [],
                  "cost": search.best.cost if search.best else None,
                  "coverage": selected_coverage.document(),
                  "search": search.document(),
                  "quotes": [quote.document() for quote in selected_quotes],
                  "budget_pressure": self.prices.budget_pressure(selected_quotes, requirement),
                  "provider_spend": self.prices.provider_breakdown(selected_quotes),
                  "selected_attribution": self.coverage.attribution(requirement, selected, evidence),
                  "cost_per_credited_sample": self.prices.cost_per_credited_sample(
                      search.best.cost, selected_coverage.total_credited) if search.best else None,
                  "verified_coverage": selected_coverage.document(),
                  "candidate": {
                      "source": "unverified_transfer_evidence" if candidate else None,
                      "selection": list(candidate.packages) if candidate else [],
                      "cost": candidate.cost if candidate else None,
                      "coverage": candidate_coverage.document() if candidate_coverage else None,
                      "quotes": [quote.document() for quote in candidate_quotes],
                      "within_budget": candidate.within_budget if candidate else None,
                      "shortfall_from_verified": (
                          candidate_coverage.total_shortage - selected_coverage.total_shortage
                          if candidate_coverage else None),
                  },
                  "evidence": self.diagnostics.evidence_inventory(offers,
                                                                  list(zip(evidence_docs, evidence))),
                  "evidence_coverage": self.diagnostics.evidence_coverage(requirement, offers,
                                                                          evidence),
                  "target_supply": self.diagnostics.target_supply(requirement, offers, evidence),
                  "eligible_supply_readiness": self.diagnostics.data_readiness(requirement, offers,
                                                                               evidence),
                  "exclusions": self.optimizer.exclusion_analysis(requirement, offers, selected,
                                                                  evidence),
                  "catalog_versions": [{"id": doc["id"], "key": doc["key"],
                                        "revision": doc["revision"], "digest": doc["digest"]}
                                       for doc, _ in offer_pairs],
                  "evidence_versions": [{"id": doc["id"], "key": doc["key"],
                                         "revision": doc["revision"], "digest": doc["digest"],
                                         "status": doc["body"].get("status")}
                                        for doc in evidence_docs]}
        return self.ledger.insert_analysis(requirement_doc, [doc for doc, _ in offer_pairs], report,
                                           evidence_docs)

    def approve(self, analysis_id: int, expected_input_digest: str) -> dict[str, Any]:
        return self.ledger.approve(analysis_id, expected_input_digest)

    def analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.analysis(analysis_id)

    def verify(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.verify(analysis_id)

    def history(self, requirement_key: str | None = None) -> list[dict[str, Any]]:
        return self.ledger.analyses(requirement_key)

    def stale_drafts(self, requirement_key: str | None = None) -> list[dict[str, Any]]:
        """列出输入已变化的未批准方案；这些方案需重新分析后才能批准。"""
        current: dict[str, str] = {}
        results = []
        for analysis in self.ledger.analyses(requirement_key):
            if analysis["state"] == "approved":
                continue
            key = analysis["requirement_key"]
            if key not in current:
                current[key] = self.ledger.latest_input_digest(key)
            if current[key] == analysis["input_digest"]:
                continue
            results.append({"analysis_id": analysis["id"], "requirement": key,
                            "requirement_revision": analysis["requirement_revision"],
                            "state": analysis["state"],
                            "snapshot_input_digest": analysis["input_digest"],
                            "current_input_digest": current[key],
                            "needs_replan": True})
        return results

    def replan(self, analysis_id: int) -> dict[str, Any]:
        """用当前目录重算一个未批准方案；已批准的历史快照不可被覆盖。"""
        previous = self.ledger.analysis(analysis_id)
        if previous["state"] == "approved":
            raise PortfolioError("approved_immutable", "approved analysis cannot be replanned")
        analysis = self.plan(previous["requirement_key"])
        analysis["supersedes"] = analysis_id
        analysis["previous_input_digest"] = previous["input_digest"]
        return analysis

    def document_history(self, kind: str, key: str) -> list[dict[str, Any]]:
        return self.catalog.history(kind, key)

    def compare_versions(self, kind: str, key: str, earlier: int, later: int) -> dict[str, Any]:
        return self.catalog.compare_versions(kind, key, earlier, later)

    def gaps(self, requirement_key: str, package_keys: list[str]) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        if any(key not in offers for key in package_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        chosen = [offers[key] for key in package_keys]
        evidence, _ = self._evidence(requirement)
        return self.coverage.gap_report(requirement, chosen, evidence)

    def marginal(self, requirement_key: str, current_keys: list[str], candidate_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        if candidate_key not in offers or any(key not in offers for key in current_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        evidence, _ = self._evidence(requirement)
        return self.coverage.marginal(requirement, [offers[key] for key in current_keys],
                                      offers[candidate_key], evidence)

    def frontier(self, requirement_key: str) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.optimizer.frontier(requirement, [offer for _, offer in self.catalog.offers()],
                                       evidence)

    def status(self) -> dict[str, Any]:
        summary = self.catalog.summary()
        analyses = self.ledger.analyses()
        return {**summary, "analysis_count": len(analyses),
                "approved_count": sum(item["state"] == "approved" for item in analyses)}

    def readiness(self, requirement_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = [offer for _, offer in self.catalog.offers()]
        evidence, evidence_docs = self._evidence(requirement)
        return {"data": self.diagnostics.data_readiness(requirement, offers, evidence),
                "paths": self.diagnostics.path_distribution(offers),
                "providers": self.diagnostics.provider_exposure(requirement, offers),
                "restrictions": self.diagnostics.licensing_reasons(requirement, offers),
                "evidence": self.diagnostics.evidence_inventory(offers,
                                                                list(zip(evidence_docs, evidence))),
                "evidence_coverage": self.diagnostics.evidence_coverage(requirement, offers,
                                                                        evidence)}

    def brief(self, requirement_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        pairs = self.catalog.evidence() if requirement.transfer_policy != "exact" else []
        return self.diagnostics.procurement_brief(requirement,
                                                   [offer for _, offer in self.catalog.offers()],
                                                   pairs)

    def budget_grid(self, requirement_key: str, budgets: list[str]) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.sensitivity.budget_grid(requirement,
                                            [offer for _, offer in self.catalog.offers()],
                                            budgets, evidence)

    def target_grid(self, requirement_key: str, target: str, minimums: list[int]) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.sensitivity.target_grid(requirement,
                                            [offer for _, offer in self.catalog.offers()],
                                            target, minimums, evidence)

    def withdrawal(self, requirement_key: str, package: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.sensitivity.remove_offer(requirement,
                                             [offer for _, offer in self.catalog.offers()],
                                             package, evidence)

    def evidence_withdrawal(self, requirement_key: str, evidence_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.sensitivity.remove_evidence(requirement,
                                                [offer for _, offer in self.catalog.offers()],
                                                evidence_key, evidence)

    def price_scenario(self, requirement_key: str, package: str, price: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        evidence, _ = self._evidence(requirement)
        return self.sensitivity.price_change(requirement,
                                             [offer for _, offer in self.catalog.offers()],
                                             package, price, evidence)

    def audit_analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.audit.explain(analysis_id)

    def compare_analyses(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        return self.audit.compare(earlier_id, later_id)

    def compare_packages(self, requirement_key: str, package_groups: list[list[str]]) -> dict[str, Any]:
        if not 1 <= len(package_groups) <= 12:
            raise PortfolioError("portfolio_count", "compare 1-12 groups")
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        evidence, _ = self._evidence(requirement)
        preferred = self.optimizer.search(requirement, list(offers.values()), evidence)
        results = []
        for position, group in enumerate(package_groups):
            if not group or len(group) != len(set(group)):
                raise PortfolioError("portfolio_shape", "each group needs distinct packages")
            if len(group) > requirement.maximum_packages:
                raise PortfolioError("portfolio_size", "group exceeds package limit")
            if any(package not in offers for package in group):
                raise PortfolioError("unknown_offer", "group includes unknown package")
            chosen = [offers[package] for package in group]
            quotes = self.prices.all_quotes(chosen, requirement)
            coverage = self.coverage.evaluate(requirement, chosen, evidence)
            rejected = [{"package": quote.package, "reason": quote.reason}
                        for quote in quotes if not quote.permitted]
            results.append({"index": position, "packages": sorted(group),
                            "cost": self.prices.total(quotes), "coverage": coverage.document(),
                            "eligible": not rejected, "restrictions": rejected,
                            "within_budget": self.prices.within_budget(quotes, requirement),
                            "feasible": not rejected and coverage.mandatory_complete
                            and self.prices.within_budget(quotes, requirement),
                            "candidate_only": (not rejected and not coverage.mandatory_complete
                                               and coverage.candidate_mandatory_complete
                                               and self.prices.within_budget(quotes, requirement)),
                            "provider_spend": self.prices.provider_breakdown(quotes),
                            "attribution": self.coverage.attribution(requirement, chosen, evidence)})
        return {"requirement": requirement.key,
                "recommended": list(preferred.best.packages) if preferred.best else [],
                "candidate_only": (list(preferred.candidate_best.packages)
                                   if not preferred.best and preferred.candidate_best else []),
                "portfolios": results}
