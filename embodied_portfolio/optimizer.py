from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from itertools import combinations
from typing import Any

from .contracts import Offer, Requirement, PortfolioError, TransferEvidence
from .coverage import CoverageEngine
from .economics import PriceEngine


@dataclass(frozen=True)
class Candidate:
    """组合评估。

    ``score``/``credited``/``mandatory_complete`` 只反映已验证供给；
    ``candidate_*`` 字段把 pending 迁移证据单独列出，绝不混入必需覆盖。
    """

    packages: tuple[str, ...]
    cost: str
    score: str
    credited: int
    shortage: int
    duplicates: int
    mandatory_complete: bool
    within_budget: bool
    candidate_score: str = "0"
    candidate_credited: int = 0
    candidate_shortage: int = 0
    candidate_duplicates: int = 0
    candidate_mandatory_complete: bool = False

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def status(self) -> str:
        if self.mandatory_complete:
            return "verified"
        if self.candidate_mandatory_complete:
            return "candidate_only"
        return "uncovered"


@dataclass(frozen=True)
class SearchResult:
    best: Candidate | None
    evaluated: int
    feasible: int
    filtered: tuple[dict[str, str], ...]
    alternatives: tuple[Candidate, ...]
    reason: str | None
    candidate_best: Candidate | None = None
    candidate_alternatives: tuple[Candidate, ...] = ()

    def document(self) -> dict[str, Any]:
        return {"best": self.best.document() if self.best else None,
                "evaluated": self.evaluated, "feasible": self.feasible,
                "filtered": list(self.filtered),
                "alternatives": [item.document() for item in self.alternatives],
                "reason": self.reason,
                "candidate_best": self.candidate_best.document() if self.candidate_best else None,
                "candidate_alternatives": [item.document() for item in self.candidate_alternatives],
                "best_status": self.best.status if self.best else (
                    self.candidate_best.status if self.candidate_best else None)}


class PortfolioOptimizer:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine, maximum_candidates: int = 18):
        self.coverage = coverage
        self.prices = prices
        self.maximum_candidates = maximum_candidates

    def evidence_for(self, requirement: Requirement,
                     evidence: list[TransferEvidence] | None) -> list[TransferEvidence]:
        if requirement.transfer_policy == "exact":
            return []
        return list(evidence or [])

    def eligible(self, requirement: Requirement, offers: list[Offer],
                 evidence: list[TransferEvidence] | None = None
                 ) -> tuple[list[Offer], list[dict[str, str]]]:
        acceptable = self.evidence_for(requirement, evidence)
        eligible = []
        filtered = []
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            if not quote.permitted:
                filtered.append({"package": offer.package, "reason": quote.reason or "not_permitted"})
                continue
            if not any(self.coverage.matches_target(offer, target, acceptable)
                       for target in requirement.targets):
                filtered.append({"package": offer.package, "reason": "no_requested_slice"})
                continue
            eligible.append(offer)
        return eligible, filtered

    def evaluate(self, requirement: Requirement, offers: list[Offer],
                 evidence: list[TransferEvidence] | None = None) -> Candidate:
        acceptable = self.evidence_for(requirement, evidence)
        coverage = self.coverage.evaluate(requirement, offers, acceptable)
        quotes = self.prices.all_quotes(offers, requirement)
        amount = self.prices.total(quotes)
        return Candidate(tuple(sorted(offer.package for offer in offers)), amount,
                         coverage.weighted_fraction, coverage.total_credited,
                         coverage.total_shortage, coverage.total_duplicate,
                         coverage.mandatory_complete, Decimal(amount) <= Decimal(requirement.budget),
                         coverage.candidate_weighted_fraction,
                         coverage.total_candidate_credited,
                         coverage.total_candidate_shortage,
                         coverage.total_candidate_duplicate,
                         coverage.candidate_mandatory_complete)

    @staticmethod
    def ranking(candidate: Candidate) -> tuple:
        """已验证方案只用已验证指标排序；候选供给只作最后的平手决胜。"""
        return (-Decimal(candidate.score), Decimal(candidate.cost),
                candidate.duplicates, len(candidate.packages), candidate.packages,
                -Decimal(candidate.candidate_score))

    @staticmethod
    def candidate_ranking(candidate: Candidate) -> tuple:
        """无法形成已验证方案时，才按候选供给潜力排序，成本仍须真实可承受。"""
        return (-Decimal(candidate.candidate_score), -Decimal(candidate.score),
                Decimal(candidate.cost), candidate.duplicates, len(candidate.packages),
                candidate.packages)

    def search(self, requirement: Requirement, offers: list[Offer],
               evidence: list[TransferEvidence] | None = None) -> SearchResult:
        acceptable = self.evidence_for(requirement, evidence)
        eligible, filtered = self.eligible(requirement, offers, acceptable)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", f"more than {self.maximum_candidates} eligible offers")
        evaluated = 0
        feasible = []
        candidate_feasible = []
        affordable = []
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                evaluated += 1
                candidate = self.evaluate(requirement, list(subset), acceptable)
                if candidate.within_budget:
                    affordable.append(candidate)
                    if candidate.mandatory_complete:
                        feasible.append(candidate)
                    elif candidate.candidate_mandatory_complete:
                        # 只把“必须依赖待确认证据才能满足必需分层”的组合单列为候选。
                        candidate_feasible.append(candidate)
        feasible.sort(key=self.ranking)
        candidate_feasible.sort(key=self.candidate_ranking)
        affordable.sort(key=self.ranking)
        if feasible:
            return SearchResult(feasible[0], evaluated, len(feasible), tuple(filtered),
                                tuple(feasible[1:4]), None,
                                candidate_feasible[0] if candidate_feasible else None,
                                tuple(candidate_feasible[1:4]))
        candidate_best = candidate_feasible[0] if candidate_feasible else None
        if not eligible:
            reason = "no_eligible_offers"
        elif not affordable:
            reason = "all_combinations_exceed_budget"
        elif candidate_best is not None:
            reason = "mandatory_targets_require_unverified_evidence"
        else:
            reason = "mandatory_targets_uncovered"
        return SearchResult(None, evaluated, 0, tuple(filtered),
                            tuple(affordable[:3]), reason, candidate_best,
                            tuple(candidate_feasible[1:4]))

    def frontier(self, requirement: Requirement, offers: list[Offer],
                 evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        acceptable = self.evidence_for(requirement, evidence)
        eligible, _ = self.eligible(requirement, offers, acceptable)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", "too many offers for frontier")
        candidates = []
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                candidate = self.evaluate(requirement, list(subset), acceptable)
                if candidate.within_budget:
                    candidates.append(candidate)
        frontier = []
        for candidate in candidates:
            dominated = any(other != candidate and
                            Decimal(other.cost) <= Decimal(candidate.cost) and
                            Decimal(other.score) >= Decimal(candidate.score) and
                            Decimal(other.candidate_score) >= Decimal(candidate.candidate_score) and
                            (Decimal(other.cost) < Decimal(candidate.cost) or
                             Decimal(other.score) > Decimal(candidate.score) or
                             Decimal(other.candidate_score) > Decimal(candidate.candidate_score))
                            for other in candidates)
            if not dominated:
                frontier.append(candidate)
        frontier.sort(key=self.ranking)
        return [item.document() for item in frontier]

    def exclusion_analysis(self, requirement: Requirement, offers: list[Offer],
                           chosen: list[Offer],
                           evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        acceptable = self.evidence_for(requirement, evidence)
        chosen_keys = {offer.package for offer in chosen}
        baseline = self.evaluate(requirement, chosen, acceptable)
        explanations = []
        for offer in offers:
            if offer.package in chosen_keys:
                continue
            quote = self.prices.quote(offer, requirement)
            if not quote.permitted:
                reason = quote.reason
                gain = None
                candidate_gain = None
            else:
                with_offer = self.evaluate(requirement, [*chosen, offer], acceptable)
                gain = with_offer.credited - baseline.credited
                candidate_gain = with_offer.candidate_credited - baseline.candidate_credited
                if not with_offer.within_budget:
                    reason = "exceeds_budget_with_selection"
                elif gain == 0 and candidate_gain == 0:
                    reason = "no_marginal_coverage"
                elif gain == 0:
                    reason = "unverified_evidence_only"
                else:
                    reason = "lower_ranked_feasible_choice"
            explanations.append({"package": offer.package, "reason": reason,
                                 "marginal_credited": gain,
                                 "marginal_candidate_credited": candidate_gain,
                                 "quote": quote.amount})
        return explanations
