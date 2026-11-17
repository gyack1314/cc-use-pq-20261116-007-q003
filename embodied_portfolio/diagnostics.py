from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .contracts import Offer, Requirement, TransferEvidence
from .coverage import CoverageEngine, effective_count, match_slice
from .economics import PriceEngine


class SupplyDiagnostics:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine):
        self.coverage = coverage
        self.prices = prices

    def path_distribution(self, offers: list[Offer]) -> dict[str, Any]:
        grouped: dict[str, list[Offer]] = defaultdict(list)
        for offer in offers:
            grouped[offer.path].append(offer)
        return {path: {"offers": len(items), "records": sum(item.sample_count for item in items),
                       "providers": len({item.provider for item in items}),
                       "hours": str(sum((Decimal(item.hours) for item in items), Decimal(0)))}
                for path, items in sorted(grouped.items())}

    def outcome_distribution(self, offers: list[Offer]) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for offer in offers:
            for observation in offer.observations:
                counts[observation.slice.outcome] += observation.count
        total = sum(counts.values())
        return {"total": total, "by_outcome": dict(sorted(counts.items())),
                "failure_and_intervention": counts["failure"] + counts["intervention"]}

    def provider_exposure(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        quotes = self.prices.all_quotes(offers, requirement)
        grouped: dict[str, list] = defaultdict(list)
        for quote in quotes:
            grouped[quote.provider].append(quote)
        results = []
        for provider, entries in sorted(grouped.items()):
            permitted = [entry for entry in entries if entry.permitted]
            results.append({"provider": provider, "offer_count": len(entries),
                            "permitted_count": len(permitted),
                            "permitted_cost": self.prices.total(permitted),
                            "restricted_packages": [entry.package for entry in entries if not entry.permitted]})
        return results

    def evidence_inventory(self, offers: list[Offer],
                           evidence: list[tuple[dict[str, Any], TransferEvidence]] | None = None
                           ) -> dict[str, Any]:
        """逐条列出迁移证据的版本、状态与适用机型，供采购方核对。"""
        items = list(evidence or [])
        package_robots = {offer.package: set(offer.robots) for offer in offers}
        rows = []
        counts = {"validated": 0, "pending": 0, "retracted": 0}
        for doc, item in items:
            status = item.status
            counts[status] = counts.get(status, 0) + 1
            reasons = []
            if item.package not in package_robots:
                reasons.append("unknown_package")
            elif item.source_robot not in package_robots[item.package]:
                reasons.append("source_robot_not_declared")
            if status == "retracted":
                reasons.append("retracted_evidence")
            rows.append({"key": item.key, "revision": doc["revision"], "digest": doc["digest"],
                         "package": item.package, "source_robot": item.source_robot,
                         "target_robot": item.target_robot,
                         "scopes": [scope.document() for scope in item.scopes],
                         "status": status, "reference": item.reference,
                         "validity": item.validity,
                         "usable": status in {"validated", "pending"} and not reasons,
                         "issues": reasons})
        return {"count": len(rows), "status_counts": counts, "evidence": rows}

    def evidence_coverage(self, requirement: Requirement, offers: list[Offer],
                          evidence: list[TransferEvidence] | None = None
                          ) -> list[dict[str, Any]]:
        """按需求分层列出已验证迁移证据与待确认候选证据分别覆盖多少。"""
        items = list(evidence or [])
        rows = []
        for target in requirement.targets:
            token = target.slice.token()
            validated, pending = [], []
            for offer in offers:
                matched = match_slice(offer, target, items)
                for observation in matched.validated:
                    validated.append({"package": offer.package,
                                      "source_robot": observation.slice.robot,
                                      "count": observation.count,
                                      "evidence": list(matched.validated_keys)})
                for observation in matched.pending:
                    pending.append({"package": offer.package,
                                    "source_robot": observation.slice.robot,
                                    "count": observation.count,
                                    "evidence": list(matched.pending_keys)})
            rows.append({"slice": token, "target_robot": target.slice.robot,
                         "required": target.minimum,
                         "validated_transfer_count": sum(item["count"] for item in validated),
                         "pending_transfer_count": sum(item["count"] for item in pending),
                         "validated_transfers": validated, "pending_transfers": pending})
        return rows

    def sparse_targets(self, requirement: Requirement, offers: list[Offer],
                       evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        result = []
        acceptable = list(evidence or []) if requirement.transfer_policy != "exact" else []
        for target in requirement.targets:
            matching: list[tuple[Offer, Any, bool]] = []
            for offer in offers:
                if not self.prices.quote(offer, requirement).permitted:
                    continue
                matched = match_slice(offer, target, acceptable)
                matching.extend((offer, obs, False) for obs in (*matched.direct, *matched.validated))
                matching.extend((offer, obs, True) for obs in matched.pending)
            verified, verified_duplicates = effective_count(
                [obs for _, obs, pending in matching if not pending])
            combined, _ = effective_count([obs for _, obs, _ in matching])
            candidate = max(0, combined - verified)
            result.append({"slice": target.slice.token(), "minimum": target.minimum,
                           "available": verified,
                           "shortage": max(0, target.minimum - verified),
                           "candidate_available": candidate,
                           "candidate_shortage": max(0, target.minimum - verified - candidate),
                           "combined_available": combined,
                           "duplicate_count": verified_duplicates,
                           "providers": sorted({offer.provider for offer, _, _ in matching}),
                           "packages": sorted({offer.package for offer, _, _ in matching})})
        return result

    def licensing_reasons(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        results: dict[str, list[str]] = defaultdict(list)
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            if quote.reason:
                for reason in quote.reason.split(","):
                    results[reason].append(offer.package)
        return {reason: sorted(packages) for reason, packages in sorted(results.items())}

    def data_readiness(self, requirement: Requirement, offers: list[Offer],
                       evidence: list[TransferEvidence] | None = None) -> dict[str, Any]:
        items = list(evidence or []) if requirement.transfer_policy != "exact" else []
        permitted = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        coverage = self.coverage.evaluate(requirement, permitted, items)
        outcomes = self.outcome_distribution(permitted)
        return {"requirement": requirement.key, "eligible_offers": len(permitted),
                "mandatory_possible": coverage.mandatory_complete,
                "mandatory_possible_with_candidates": coverage.candidate_mandatory_complete,
                "target_shortage": coverage.total_shortage,
                "candidate_shortage": coverage.total_candidate_shortage,
                "known_duplicates": coverage.total_duplicate,
                "outcomes": outcomes["by_outcome"],
                "sparse_targets": self.sparse_targets(requirement, permitted, items),
                "evidence_coverage": self.evidence_coverage(requirement, permitted, items)}

    def offer_scorecards(self, requirement: Requirement, offers: list[Offer],
                         evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        """逐包展示独立贡献，不能据此代替组合决策。"""
        items = list(evidence or []) if requirement.transfer_policy != "exact" else []
        cards = []
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            coverage = self.coverage.evaluate(requirement, [offer], items)
            matched = [cell for cell in coverage.cells if cell.available]
            missing = [cell.slice for cell in coverage.cells if cell.shortage and cell.mandatory]
            candidate_missing = [cell.slice for cell in coverage.cells
                                 if cell.candidate_shortage and cell.mandatory]
            cards.append({"package": offer.package, "provider": offer.provider,
                          "path": offer.path, "sample_count": offer.sample_count,
                          "target_count": len(matched),
                          "credited": coverage.total_credited,
                          "candidate_credited": coverage.total_candidate_credited,
                          "mandatory_shortage": sum(cell.shortage for cell in coverage.cells if cell.mandatory),
                          "candidate_mandatory_shortage": sum(cell.candidate_shortage
                                                              for cell in coverage.cells if cell.mandatory),
                          "mandatory_gaps": missing,
                          "candidate_mandatory_gaps": candidate_missing,
                          "transferred_available": sum(cell.transferred_available
                                                       for cell in coverage.cells),
                          "weighted_fraction": coverage.weighted_fraction,
                          "candidate_weighted_fraction": coverage.candidate_weighted_fraction,
                          "price": quote.amount, "price_mode": quote.mode,
                          "price_quantity": quote.quantity,
                          "permitted": quote.permitted, "restriction": quote.reason,
                          "cost_per_credited": self.prices.cost_per_credited_sample(
                              quote.amount, coverage.total_credited)})
        return sorted(cards, key=lambda item: item["package"])

    def overlap_network(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        """比较已声明的样本标识，并揭示无法确认的重复。"""
        eligible = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        targets = {target.slice.token() for target in requirement.targets}
        edges = []
        for left_index, left in enumerate(eligible):
            for right in eligible[left_index + 1:]:
                raw = self.coverage.pair_overlap(left, right)
                relevant = [row for row in raw["shared_slices"] if row["slice"] in targets]
                if not relevant:
                    continue
                edges.append({"first": left.package, "second": right.package,
                              "known_overlap": sum(row["known_overlap"] for row in relevant),
                              "shared_target_count": len(relevant),
                              "unverified_target_count": sum(not row["identifiers_complete"]
                                                             for row in relevant),
                              "targets": relevant})
        edges.sort(key=lambda item: (item["first"], item["second"]))
        return {"eligible_packages": [offer.package for offer in eligible],
                "edges": edges,
                "known_overlap_total": sum(edge["known_overlap"] for edge in edges),
                "unverified_pairs": sum(edge["unverified_target_count"] > 0 for edge in edges)}

    def target_supply(self, requirement: Requirement, offers: list[Offer],
                      evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        """并列展示逐包供给与去重后的总有效供给；迁移供给单独标注。"""
        items = list(evidence or []) if requirement.transfer_policy != "exact" else []
        eligible = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        sparse = {item["slice"]: item for item in self.sparse_targets(requirement, eligible, items)}
        rows = []
        for target in requirement.targets:
            token = target.slice.token()
            contributors = []
            for offer in eligible:
                matched = match_slice(offer, target, items)
                if not (matched.direct or matched.validated or matched.pending):
                    continue
                quote = self.prices.quote(offer, requirement)
                contributors.append({"package": offer.package, "provider": offer.provider,
                                     "count": sum(obs.count for obs in matched.direct),
                                     "validated_transfer_count": sum(obs.count for obs in matched.validated),
                                     "pending_transfer_count": sum(obs.count for obs in matched.pending),
                                     "verified_count": sum(obs.count for obs in matched.direct)
                                     + sum(obs.count for obs in matched.validated),
                                     "identifiers_known": bool(any(obs.episode_ids for obs in
                                                                  (*matched.direct, *matched.validated))),
                                     "whole_package_price": quote.amount})
            aggregate = sparse[token]
            rows.append({"slice": token, "minimum": target.minimum,
                         "mandatory": target.mandatory, "weight": target.weight,
                         "available": aggregate["available"],
                         "shortage": aggregate["shortage"],
                         "candidate_available": aggregate["candidate_available"],
                         "candidate_shortage": aggregate["candidate_shortage"],
                         "combined_available": aggregate["combined_available"],
                         "duplicates": aggregate["duplicate_count"],
                         "contributor_count": len(contributors),
                         "contributors": sorted(contributors, key=lambda item: item["package"])})
        return rows

    def procurement_brief(self, requirement: Requirement, offers: list[Offer],
                          evidence: list[tuple[dict[str, Any], TransferEvidence]] | None = None
                          ) -> dict[str, Any]:
        items = [item for _, item in (evidence or [])] if requirement.transfer_policy != "exact" else []
        readiness = self.data_readiness(requirement, offers, items)
        cards = self.offer_scorecards(requirement, offers, items)
        overlaps = self.overlap_network(requirement, offers)
        supply = self.target_supply(requirement, offers, items)
        return {"requirement": requirement.key, "client": requirement.client,
                "budget": requirement.budget, "transfer_policy": requirement.transfer_policy,
                "offer_count": len(offers), "eligible_offer_count": readiness["eligible_offers"],
                "mandatory_possible_from_all_supply": readiness["mandatory_possible"],
                "mandatory_possible_with_candidates": readiness["mandatory_possible_with_candidates"],
                "supply_shortage": readiness["target_shortage"],
                "candidate_shortage": readiness["candidate_shortage"],
                "known_duplicate_records": readiness["known_duplicates"],
                "paths": self.path_distribution(offers),
                "outcomes": self.outcome_distribution(offers),
                "restrictions": self.licensing_reasons(requirement, offers),
                "evidence": self.evidence_inventory(offers, evidence),
                "evidence_coverage": self.evidence_coverage(requirement, offers, items),
                "offer_scorecards": cards, "target_supply": supply,
                "overlaps": overlaps}
