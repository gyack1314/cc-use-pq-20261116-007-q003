from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any
import json
import sys

from .contracts import PortfolioError
from .decision import PortfolioService


def parser() -> ArgumentParser:
    main = ArgumentParser(prog="embodied_portfolio", description="离线具身数据采购组合分析")
    main.add_argument("--db", default="portfolio.sqlite3", help="本地SQLite文件")
    commands = main.add_subparsers(dest="command", required=True)
    incoming = commands.add_parser("import")
    incoming.add_argument("kind", choices=("offer", "requirement", "evidence"))
    incoming.add_argument("file")
    batch = commands.add_parser("batch")
    batch.add_argument("file")
    plan = commands.add_parser("plan")
    plan.add_argument("requirement")
    approve = commands.add_parser("approve")
    approve.add_argument("analysis", type=int)
    approve.add_argument("input_digest")
    analysis = commands.add_parser("analysis")
    analysis.add_argument("id", type=int)
    verify = commands.add_parser("verify")
    verify.add_argument("id", type=int)
    history = commands.add_parser("history")
    history.add_argument("--requirement")
    stale = commands.add_parser("stale-drafts")
    stale.add_argument("--requirement")
    replan = commands.add_parser("replan")
    replan.add_argument("analysis", type=int)
    documents = commands.add_parser("versions")
    documents.add_argument("kind", choices=("offer", "requirement", "evidence"))
    documents.add_argument("key")
    revisions = commands.add_parser("compare-versions")
    revisions.add_argument("kind", choices=("offer", "requirement", "evidence"))
    revisions.add_argument("key")
    revisions.add_argument("earlier", type=int)
    revisions.add_argument("later", type=int)
    gaps = commands.add_parser("gaps")
    gaps.add_argument("requirement")
    gaps.add_argument("packages", nargs="*")
    marginal = commands.add_parser("marginal")
    marginal.add_argument("requirement")
    marginal.add_argument("candidate")
    marginal.add_argument("current", nargs="*")
    frontier = commands.add_parser("frontier")
    frontier.add_argument("requirement")
    readiness = commands.add_parser("readiness")
    readiness.add_argument("requirement")
    brief = commands.add_parser("brief")
    brief.add_argument("requirement")
    budgets = commands.add_parser("budgets")
    budgets.add_argument("requirement")
    budgets.add_argument("amounts", nargs="+")
    targets = commands.add_parser("targets")
    targets.add_argument("requirement")
    targets.add_argument("slice")
    targets.add_argument("minimums", nargs="+", type=int)
    withdrawal = commands.add_parser("withdrawal")
    withdrawal.add_argument("requirement")
    withdrawal.add_argument("package")
    evidence_withdrawal = commands.add_parser("evidence-withdrawal")
    evidence_withdrawal.add_argument("requirement")
    evidence_withdrawal.add_argument("evidence")
    price = commands.add_parser("price-scenario")
    price.add_argument("requirement")
    price.add_argument("package")
    price.add_argument("unit_price")
    audit = commands.add_parser("audit")
    audit.add_argument("id", type=int)
    compare = commands.add_parser("compare")
    compare.add_argument("earlier", type=int)
    compare.add_argument("later", type=int)
    groups = commands.add_parser("compare-packages")
    groups.add_argument("requirement")
    groups.add_argument("groups", nargs="+", help="comma-separated package keys per portfolio")
    events = commands.add_parser("events")
    events.add_argument("--key")
    commands.add_parser("status")
    demo = commands.add_parser("demo")
    demo.add_argument("--workspace", default="fixtures")
    transfer_demo = commands.add_parser("demo-transfer")
    transfer_demo.add_argument("--workspace", default="fixtures_transfer")
    return main


def demo_payloads() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}
    requirement = {"key": "warehouse_trial", "client": "client_a", "title": "合成仓储抓取失败样本采购",
                   "budget": "180", "targets": [{"slice": target, "minimum": 4,
                                                  "mandatory": True, "weight": "2"}],
                   "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 3}
    first = {"provider": "provider_a", "package": "remote_01", "path": "teleoperation",
             "robots": ["arm_a"], "observations": [{"slice": target, "count": 3,
                                                  "episode_ids": ["trial_a", "trial_b", "trial_c"]}],
             "hours": "1", "price_mode": "per_hour", "unit_price": "100",
             "license": "nonexclusive", "allowed_clients": [], "validity": "2026", "notes": "合成数据"}
    second = {"provider": "provider_b", "package": "portable_02", "path": "portable",
              "robots": ["arm_a"], "observations": [{"slice": target, "count": 2,
                                                   "episode_ids": ["trial_c", "trial_d"]}],
              "hours": "0", "price_mode": "flat", "unit_price": "50",
              "license": "single_customer", "allowed_clients": ["client_a"],
              "validity": "2026", "notes": "合成数据"}
    return requirement, [first, second]


def transfer_demo_payloads() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """合成演示：便携采集的 arm_b 失败样本，仅抓取阶段有已验证迁移记录。"""
    target = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}
    pending_target = {**target, "stage": "place"}
    requirement = {"key": "transfer_trial", "client": "client_a",
                   "title": "便携失败样本迁移采购（合成）", "budget": "220",
                   "targets": [{"slice": target, "minimum": 4, "mandatory": True, "weight": "2"},
                               {"slice": pending_target, "minimum": 2, "mandatory": True,
                                "weight": "1"}],
                   "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 2,
                   "transfer_policy": "validated"}
    direct = {"provider": "provider_a", "package": "teleop_direct", "path": "teleoperation",
              "robots": ["arm_a"], "observations": [{"slice": target, "count": 2,
                                                   "episode_ids": ["a_01", "a_02"]}],
              "hours": "1", "price_mode": "per_hour", "unit_price": "60",
              "license": "nonexclusive", "allowed_clients": [], "validity": "2026",
              "notes": "合成数据"}
    portable_target = {**target, "robot": "arm_b"}
    portable = {"provider": "provider_b", "package": "portable_transfer", "path": "portable",
                "robots": ["arm_b"],
                "observations": [{"slice": portable_target,
                                  "count": 4, "episode_ids": ["b_01", "b_02", "b_03", "b_04"]},
                                 {"slice": {**portable_target, "stage": "place"},
                                  "count": 2, "episode_ids": ["b_10", "b_11"]}],
                "hours": "0", "price_mode": "flat", "unit_price": "60",
                "license": "nonexclusive", "allowed_clients": [], "validity": "2026",
                "notes": "便携设备采集"}
    evidence = [
        {"key": "transfer_grasp_v1", "package": "portable_transfer",
         "source_robot": "arm_b", "target_robot": "arm_a",
         "scopes": [{"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure"}],
         "status": "validated", "reference": "bench-report-2026-01", "validity": "2026",
         "notes": "已验证抓取阶段迁移"},
        {"key": "transfer_place_v1", "package": "portable_transfer",
         "source_robot": "arm_b", "target_robot": "arm_a",
         "scopes": [{"scenario": "warehouse_pick", "stage": "place", "outcome": "failure"}],
         "status": "pending", "reference": "supplier-claim", "validity": "2026",
         "notes": "供应商声称可迁移，尚无验证记录"},
    ]
    return requirement, [direct, portable], evidence


def transfer_demo(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    requirement, offers, evidence = transfer_demo_payloads()
    with PortfolioService(root / "portfolio.sqlite3") as service:
        service.import_requirement(requirement)
        for offer in offers:
            service.import_offer(offer)
        for item in evidence:
            service.import_evidence(item)
        analysis = service.plan(requirement["key"])
        report = analysis["report"]
        return {"analysis_id": analysis["id"], "state": analysis["state"],
                "feasible": report["feasible"], "selection": report["selection"],
                "cost": report["cost"],
                "verified_credited": report["verified_coverage"]["total_credited"],
                "candidate_selection": report["candidate"]["selection"],
                "candidate_cost": report["candidate"]["cost"],
                "candidate_credited_with_pending": (
                    report["candidate"]["coverage"]["total_candidate_credited"]
                    if report["candidate"]["coverage"] else 0),
                "targets": [{"slice": row["slice"], "required": row["required"],
                             "validated_transfer_count": row["validated_transfer_count"],
                             "pending_transfer_count": row["pending_transfer_count"]}
                            for row in report["evidence_coverage"]],
                "evidence": [{"key": item["key"], "status": item["status"], "usable": item["usable"]}
                             for item in report["evidence"]["evidence"]]}


def demo(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    requirement, offers = demo_payloads()
    with PortfolioService(root / "portfolio.sqlite3") as service:
        req = service.import_requirement(requirement)
        for offer in offers:
            service.import_offer(offer)
        analysis = service.plan(req["key"])
        if not analysis["report"]["feasible"]:
            raise PortfolioError("demo_infeasible", "synthetic example unexpectedly infeasible")
        approved = service.approve(analysis["id"], analysis["input_digest"])
        verified = service.verify(approved["id"])
        if not verified["verified"]:
            raise PortfolioError("demo_verify", "synthetic analysis snapshot did not verify")
        return {"analysis_id": approved["id"], "state": approved["state"],
                "selection": approved["report"]["selection"], "cost": approved["report"]["cost"],
                "credited": approved["report"]["coverage"]["total_credited"],
                "duplicate_count": approved["report"]["coverage"]["total_duplicate"],
                "verified": verified["verified"]}


def execute(args: Namespace) -> Any:
    if args.command == "demo":
        return demo(args.workspace)
    if args.command == "demo-transfer":
        return transfer_demo(args.workspace)
    with PortfolioService(args.db) as service:
        if args.command == "import":
            return service.import_file(args.kind, args.file)
        if args.command == "batch":
            return service.import_batch_file(args.file)
        if args.command == "plan":
            return service.plan(args.requirement)
        if args.command == "approve":
            return service.approve(args.analysis, args.input_digest)
        if args.command == "analysis":
            return service.analysis(args.id)
        if args.command == "verify":
            return service.verify(args.id)
        if args.command == "history":
            return service.history(args.requirement)
        if args.command == "stale-drafts":
            return service.stale_drafts(args.requirement)
        if args.command == "replan":
            return service.replan(args.analysis)
        if args.command == "versions":
            return service.document_history(args.kind, args.key)
        if args.command == "compare-versions":
            return service.compare_versions(args.kind, args.key, args.earlier, args.later)
        if args.command == "gaps":
            return service.gaps(args.requirement, args.packages)
        if args.command == "marginal":
            return service.marginal(args.requirement, args.current, args.candidate)
        if args.command == "frontier":
            return service.frontier(args.requirement)
        if args.command == "readiness":
            return service.readiness(args.requirement)
        if args.command == "brief":
            return service.brief(args.requirement)
        if args.command == "budgets":
            return service.budget_grid(args.requirement, args.amounts)
        if args.command == "targets":
            return service.target_grid(args.requirement, args.slice, args.minimums)
        if args.command == "withdrawal":
            return service.withdrawal(args.requirement, args.package)
        if args.command == "evidence-withdrawal":
            return service.evidence_withdrawal(args.requirement, args.evidence)
        if args.command == "price-scenario":
            return service.price_scenario(args.requirement, args.package, args.unit_price)
        if args.command == "audit":
            return service.audit_analysis(args.id)
        if args.command == "compare":
            return service.compare_analyses(args.earlier, args.later)
        if args.command == "compare-packages":
            return service.compare_packages(args.requirement, [item.split(",") for item in args.groups])
        if args.command == "events":
            return service.ledger.events(args.key)
        if args.command == "status":
            return service.status()
    raise PortfolioError("command", "unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(json.dumps(execute(args), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (PortfolioError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
