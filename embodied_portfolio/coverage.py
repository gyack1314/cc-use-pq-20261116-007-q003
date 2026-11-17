from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Iterable

from .contracts import Observation, Offer, Requirement, Target, TransferEvidence


@dataclass(frozen=True)
class CoverageCell:
    """逐层覆盖。

    ``credited``/``shortage`` 只按已验证供给计算；带 pending 证据的迁移样本进入
    ``candidate_*`` 字段，供客户区分“已达必需覆盖”与“仅候选供给”。
    """

    slice: str
    requested: int
    available: int
    credited: int
    shortage: int
    mandatory: bool
    weight: str
    packages: tuple[str, ...]
    duplicate_count: int
    direct_available: int = 0
    transferred_available: int = 0
    candidate_available: int = 0
    candidate_credited: int = 0
    candidate_shortage: int = 0
    direct_packages: tuple[str, ...] = ()
    transferred_packages: tuple[str, ...] = ()
    candidate_packages: tuple[str, ...] = ()
    evidence_keys: tuple[str, ...] = ()
    candidate_evidence_keys: tuple[str, ...] = ()
    status: str = "uncovered"

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageResult:
    cells: tuple[CoverageCell, ...]
    mandatory_complete: bool
    weighted_fraction: str
    total_credited: int
    total_shortage: int
    total_duplicate: int
    candidate_mandatory_complete: bool = False
    candidate_weighted_fraction: str = "0"
    total_candidate_credited: int = 0
    total_candidate_shortage: int = 0
    total_candidate_duplicate: int = 0

    def document(self) -> dict[str, Any]:
        return {"cells": [cell.document() for cell in self.cells],
                "mandatory_complete": self.mandatory_complete,
                "candidate_mandatory_complete": self.candidate_mandatory_complete,
                "weighted_fraction": self.weighted_fraction,
                "candidate_weighted_fraction": self.candidate_weighted_fraction,
                "total_credited": self.total_credited,
                "total_shortage": self.total_shortage,
                "total_duplicate": self.total_duplicate,
                "total_candidate_credited": self.total_candidate_credited,
                "total_candidate_shortage": self.total_candidate_shortage,
                "total_candidate_duplicate": self.total_candidate_duplicate}


def observed_for(offer: Offer, slice_key: str) -> Observation | None:
    for observation in offer.observations:
        if observation.slice.token() == slice_key:
            return observation
    return None


def effective_count(items: list[Observation]) -> tuple[int, int]:
    if not items:
        return 0, 0
    known: set[str] = set()
    anonymous = 0
    nominal = 0
    for item in items:
        nominal += item.count
        if item.episode_ids:
            known.update(item.episode_ids)
        else:
            anonymous += item.count
    unique = anonymous + len(known)
    return unique, nominal - unique


def evidence_applies(offer: Offer, observation: Observation, target: Target,
                     evidence: TransferEvidence) -> bool:
    """一条迁移证据是否真的能把该观测用于目标分层。

    机型必须逐字一致：观测中的源机型 == 证据声明的源机型，需求目标机型 ==
    证据声明的目标机型，且源机型必须已在该包中声明。场景/阶段/结果也必须一致。
    """
    if evidence.status not in {"validated", "pending"}:
        return False
    if evidence.package != offer.package:
        return False
    if observation.slice.robot not in offer.robots:
        return False
    if evidence.source_robot != observation.slice.robot:
        return False
    if evidence.target_robot != target.slice.robot:
        return False
    if observation.slice.robot == target.slice.robot:
        return False
    if (observation.slice.scenario, observation.slice.stage, observation.slice.outcome) != (
            target.slice.scenario, target.slice.stage, target.slice.outcome):
        return False
    return evidence.covers(target.slice.scenario, target.slice.stage, target.slice.outcome)


def best_transfer_evidence(offer: Offer, observation: Observation, target: Target,
                           evidence: Iterable[TransferEvidence]) -> str | None:
    """同一观测可能被多条证据覆盖；validated 优先于 pending。"""
    pending = False
    for item in evidence:
        if not evidence_applies(offer, observation, target, item):
            continue
        if item.status == "validated":
            return "validated"
        pending = True
    return "pending" if pending else None


@dataclass(frozen=True)
class MatchedSlice:
    offer: Offer
    direct: tuple[Observation, ...]
    validated: tuple[Observation, ...]
    pending: tuple[Observation, ...]
    validated_keys: tuple[str, ...]
    pending_keys: tuple[str, ...]


def match_slice(offer: Offer, target: Target, evidence: Iterable[TransferEvidence]) -> MatchedSlice:
    direct: list[Observation] = []
    validated: list[Observation] = []
    pending: list[Observation] = []
    validated_keys: set[str] = set()
    pending_keys: set[str] = set()
    items = list(evidence)
    for observation in offer.observations:
        if observation.slice.robot == target.slice.robot:
            if observation.slice.token() == target.slice.token():
                direct.append(observation)
            continue
        if (observation.slice.scenario, observation.slice.stage, observation.slice.outcome) != (
                target.slice.scenario, target.slice.stage, target.slice.outcome):
            continue
        status = best_transfer_evidence(offer, observation, target, items)
        if status is None:
            continue
        if status == "validated":
            validated.append(observation)
            validated_keys.update(item.key for item in items
                                  if item.status == "validated"
                                  and evidence_applies(offer, observation, target, item))
        else:
            pending.append(observation)
            pending_keys.update(item.key for item in items
                                if item.status == "pending"
                                and evidence_applies(offer, observation, target, item))
    return MatchedSlice(offer, tuple(direct), tuple(validated), tuple(pending),
                        tuple(sorted(validated_keys)), tuple(sorted(pending_keys)))


class CoverageEngine:
    def evaluate(self, requirement: Requirement, offers: list[Offer],
                 evidence: list[TransferEvidence] | None = None) -> CoverageResult:
        items = list(evidence or [])
        accepted = requirement.transfer_policy != "exact"
        if not accepted:
            items = []
        cells = []
        earned = Decimal(0)
        candidate_earned = Decimal(0)
        possible = Decimal(0)
        for target in requirement.targets:
            direct_items: list[Observation] = []
            validated_items: list[Observation] = []
            pending_items: list[Observation] = []
            direct_packages: set[str] = set()
            transferred_packages: set[str] = set()
            candidate_packages: set[str] = set()
            validated_keys: set[str] = set()
            pending_keys: set[str] = set()
            for offer in offers:
                matched = match_slice(offer, target, items)
                direct_items.extend(matched.direct)
                validated_items.extend(matched.validated)
                pending_items.extend(matched.pending)
                if matched.direct:
                    direct_packages.add(offer.package)
                if matched.validated:
                    transferred_packages.add(offer.package)
                if matched.pending:
                    candidate_packages.add(offer.package)
                validated_keys.update(matched.validated_keys)
                pending_keys.update(matched.pending_keys)
            verified_items = [*direct_items, *validated_items]
            available, duplicate = effective_count(verified_items)
            combined_available, combined_duplicate = effective_count([*verified_items, *pending_items])
            candidate_available = max(0, combined_available - available)
            candidate_duplicate = max(0, combined_duplicate - duplicate)
            credited = min(available, target.minimum)
            shortage = target.minimum - credited
            candidate_credited = min(candidate_available, shortage)
            candidate_shortage = shortage - candidate_credited
            weight = Decimal(target.weight)
            earned += weight * Decimal(credited) / Decimal(target.minimum)
            candidate_earned += weight * Decimal(credited + candidate_credited) / Decimal(target.minimum)
            possible += weight
            if shortage == 0:
                status = "covered"
            elif candidate_shortage == 0:
                status = "candidate_only"
            else:
                status = "uncovered"
            direct_available, _ = effective_count(direct_items)
            transferred_available = max(0, available - direct_available)
            cells.append(CoverageCell(
                target.slice.token(), target.minimum, available, credited, shortage,
                target.mandatory, target.weight,
                tuple(sorted(direct_packages | transferred_packages)), duplicate,
                direct_available=direct_available,
                transferred_available=transferred_available,
                candidate_available=candidate_available,
                candidate_credited=candidate_credited,
                candidate_shortage=candidate_shortage,
                direct_packages=tuple(sorted(direct_packages)),
                transferred_packages=tuple(sorted(transferred_packages)),
                candidate_packages=tuple(sorted(candidate_packages)),
                evidence_keys=tuple(sorted(validated_keys)),
                candidate_evidence_keys=tuple(sorted(pending_keys)),
                status=status))
        complete = all(not cell.mandatory or cell.shortage == 0 for cell in cells)
        candidate_complete = all(not cell.mandatory or cell.candidate_shortage == 0 for cell in cells)
        fraction = earned / possible if possible else Decimal(0)
        candidate_fraction = candidate_earned / possible if possible else Decimal(0)
        return CoverageResult(tuple(cells), complete, str(fraction),
                              sum(cell.credited for cell in cells),
                              sum(cell.shortage for cell in cells),
                              sum(cell.duplicate_count for cell in cells),
                              candidate_complete, str(candidate_fraction),
                              sum(cell.candidate_credited for cell in cells),
                              sum(cell.candidate_shortage for cell in cells),
                              sum(cell.candidate_available for cell in cells))

    def matches_target(self, offer: Offer, target: Target,
                       evidence: list[TransferEvidence] | None = None,
                       *, accepted: bool = True) -> bool:
        """该包是否对目标分层有直接或迁移供给（pending 也算候选）。"""
        items = list(evidence or []) if accepted else []
        matched = match_slice(offer, target, items)
        return bool(matched.direct or matched.validated or matched.pending)

    def offer_matrix(self, requirement: Requirement, offers: list[Offer],
                     evidence: list[TransferEvidence] | None = None) -> dict[str, Any]:
        rows = []
        items = list(evidence or [])
        for offer in offers:
            row = {"package": offer.package, "path": offer.path, "provider": offer.provider,
                   "slices": {}, "verified_slices": {}, "candidate_slices": {}}
            for target in requirement.targets:
                token = target.slice.token()
                matched = match_slice(offer, target, items if requirement.transfer_policy != "exact" else [])
                direct = observed_for(offer, token)
                row["slices"][token] = direct.count if direct else 0
                row["verified_slices"][token] = sum(item.count for item in matched.direct) + \
                    sum(item.count for item in matched.validated)
                row["candidate_slices"][token] = sum(item.count for item in matched.pending)
            rows.append(row)
        return {"targets": [target.slice.token() for target in requirement.targets], "offers": rows}

    def marginal(self, requirement: Requirement, current: list[Offer], candidate: Offer,
                 evidence: list[TransferEvidence] | None = None) -> dict[str, Any]:
        before = self.evaluate(requirement, current, evidence)
        after = self.evaluate(requirement, [*current, candidate], evidence)
        changes = []
        for old, new in zip(before.cells, after.cells):
            changes.append({"slice": old.slice, "credited_gain": new.credited - old.credited,
                            "candidate_credited_gain": new.candidate_credited - old.candidate_credited,
                            "duplicate_increase": new.duplicate_count - old.duplicate_count,
                            "remaining_shortage": new.shortage,
                            "remaining_candidate_shortage": new.candidate_shortage})
        return {"package": candidate.package, "credited_gain": after.total_credited - before.total_credited,
                "candidate_credited_gain": (after.total_candidate_credited
                                            - before.total_candidate_credited),
                "duplicate_increase": after.total_duplicate - before.total_duplicate,
                "weighted_fraction_before": before.weighted_fraction,
                "weighted_fraction_after": after.weighted_fraction,
                "candidate_weighted_fraction_after": after.candidate_weighted_fraction,
                "changes": changes}

    def pair_overlap(self, first: Offer, second: Offer) -> dict[str, Any]:
        shared = []
        for left in first.observations:
            right = observed_for(second, left.slice.token())
            if right is None:
                continue
            repeated = len(set(left.episode_ids) & set(right.episode_ids))
            shared.append({"slice": left.slice.token(), "first_count": left.count,
                           "second_count": right.count, "known_overlap": repeated,
                           "identifiers_complete": bool(left.episode_ids and right.episode_ids)})
        return {"first": first.package, "second": second.package,
                "shared_slices": shared,
                "known_overlap_total": sum(item["known_overlap"] for item in shared)}

    def gap_report(self, requirement: Requirement, offers: list[Offer],
                   evidence: list[TransferEvidence] | None = None) -> dict[str, Any]:
        result = self.evaluate(requirement, offers, evidence)
        gaps = []
        for cell in result.cells:
            if not cell.shortage and not cell.candidate_shortage:
                continue
            gaps.append({"slice": cell.slice, "missing": cell.shortage,
                         "mandatory": cell.mandatory, "providers": list(cell.packages),
                         "candidate_missing": cell.candidate_shortage,
                         "candidate_providers": list(cell.candidate_packages),
                         "status": cell.status})
        return {"requirement": requirement.key, "mandatory_complete": result.mandatory_complete,
                "candidate_mandatory_complete": result.candidate_mandatory_complete,
                "gaps": gaps, "total_shortage": result.total_shortage,
                "total_candidate_shortage": result.total_candidate_shortage}

    def attribution(self, requirement: Requirement, offers: list[Offer],
                    evidence: list[TransferEvidence] | None = None) -> list[dict[str, Any]]:
        result = self.evaluate(requirement, offers, evidence)
        output = []
        for offer in offers:
            without = [item for item in offers if item.package != offer.package]
            before = self.evaluate(requirement, without, evidence)
            output.append({"package": offer.package,
                           "exclusive_credited": result.total_credited - before.total_credited,
                           "exclusive_candidate_credited": (result.total_candidate_credited
                                                           - before.total_candidate_credited),
                           "mandatory_would_fail": result.mandatory_complete and not before.mandatory_complete})
        return output


def describe_slice(token: str) -> dict[str, str]:
    scenario, stage, outcome, robot = token.split("|", 3)
    return {"scenario": scenario, "stage": stage, "outcome": outcome, "robot": robot}
