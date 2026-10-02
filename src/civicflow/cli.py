"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .errors import CivicFlowError
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def portfolio_demo(app: CivicFlow) -> dict:
    book = app.portfolio
    guide = book.freeze_guideline(guideline_id="guide:2027-rd", title="2027 年度研发计划适用指南", content={"chapter": "组合管理", "rules": ["指南与预算版本立项即冻结"]}, actor="demo-operator")
    plan = book.create_unit(unit_type="plan", code="PLAN-2027", name="省重点研发计划", guideline=("guide:2027-rd", guide["version"]), actor="demo-operator")
    program = book.create_unit(unit_type="program", code="SP-NEWMAT", name="新材料专题", parent_id=plan["unit_id"], actor="demo-operator")
    project = book.create_unit(unit_type="project", code="PRJ-ALLOY", name="高强合金项目", parent_id=program["unit_id"], leader="person:pm", actor="demo-operator")
    topic_a = book.create_unit(unit_type="topic", code="T-A", name="合金配方课题", parent_id=project["unit_id"], leader="person:lead-a", actor="demo-operator")
    topic_b = book.create_unit(unit_type="topic", code="T-B", name="中试工艺课题", parent_id=project["unit_id"], leader="person:lead-b", actor="demo-operator")
    book.add_participant(org_id="org:univ", unit_id=topic_a["unit_id"], role="lead")
    book.add_participant(org_id="org:firm", unit_id=topic_b["unit_id"], role="collaborator")
    budget = book.freeze_budget(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
        {"topic_id": topic_a["unit_id"], "source_kind": "national", "source_ref": "NAT-2027-01", "amount": "600000.00"},
        {"topic_id": topic_a["unit_id"], "source_kind": "provincial", "source_ref": "PROV-2027-07", "amount": "300000.00"},
        {"topic_id": topic_b["unit_id"], "source_kind": "enterprise", "source_ref": "FIRM-MATCH-9", "amount": "400000.00"},
    ], actor="demo-operator")
    book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="配方定型", leader="person:lead-a", due_at="2027-06-30T18:00:00+08:00", hold_amount="200000.00")
    milestone_b = book.add_milestone(topic_id=topic_b["unit_id"], code="M1", title="中试线打通", leader="person:lead-b", due_at="2027-09-30T18:00:00+08:00", hold_amount="150000.00")
    book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="600000.00", reference="grant-nat-1", source_kind="national", source_ref="NAT-2027-01", actor="treasury")
    book.post_fund(topic_id=topic_a["unit_id"], kind="matching", amount="300000.00", reference="grant-prov-1", source_kind="provincial", source_ref="PROV-2027-07", actor="treasury")
    book.post_fund(topic_id=topic_b["unit_id"], kind="matching", amount="400000.00", reference="grant-firm-1", source_kind="enterprise", source_ref="FIRM-MATCH-9", actor="treasury")
    usage = book.record_facility_use(facility_id="facility:sem", topic_id=topic_a["unit_id"], start_at="2027-03-01T09:00:00+08:00", end_at="2027-03-01T12:00:00+08:00", charge="8000.00", charge_reference="SEM-20270301-01", used_by="person:lead-a")
    charged = book.charge_facility_use(usage["usage_id"], actor="demo-operator")
    book.occupy(asset_kind="sample", asset_id="sample:rare-01", topic_id=topic_a["unit_id"], holder="person:lead-a", start_at="2027-03-01T08:00:00+08:00", end_at="2027-03-02T08:00:00+08:00")
    material = book.register_material(topic_id=topic_a["unit_id"], title="合金配方数据包", classification="restricted", fields=["composition", "process", "raw_data"])
    book.grant_material(material_id=material["material_id"], org_id="org:firm", fields=["composition"], valid_until="2027-12-31T23:59:59+08:00")
    ip = book.register_ip(title="高强合金配方专利", kind="patent", topic_id=topic_a["unit_id"])
    contract = book.register_contract(title="合金技术转让合同", amount="1000000.00", refs={"national": ["NAT-2027-01"], "provincial": ["PROV-2027-07"], "enterprise": ["FIRM-MATCH-9"]}, topic_shares={topic_a["unit_id"]: "60.00", topic_b["unit_id"]: "40.00"})
    contributions = book.save_contributions(result_type="contract", result_id=contract["contract_id"], shares={project["unit_id"]: "100.00"})
    adjustment = book.propose_adjustment(kind="extension", topic_id=topic_b["unit_id"], applicant="person:lead-b", payload={"days": 30, "reason": "设备到货延迟"})
    own_review = None
    try:
        book.approve_adjustment(adjustment["adjustment_id"], reviewer="person:lead-b", reason="同意")
    except CivicFlowError as exc:
        own_review = str(exc)
    book.approve_adjustment(adjustment["adjustment_id"], reviewer="person:dean", reason="情况属实，准予延期 30 天")
    applied = book.apply_adjustment(adjustment["adjustment_id"])
    book.receive_acceptance(milestone_id=milestone_b["milestone_id"], receipt_id="RCV-2027-1001", amount="150000.00")
    first_release = book.process_acceptance("RCV-2027-1001", actor="demo-operator")
    replay_release = book.process_acceptance("RCV-2027-1001", actor="demo-operator")
    duplicate_receipt = None
    try:
        book.receive_acceptance(milestone_id=milestone_b["milestone_id"], receipt_id="RCV-2027-1001", amount="150000.00")
    except CivicFlowError as exc:
        duplicate_receipt = str(exc)
    open_conflict = book.conflicts(state="open")[0]
    book.resolve_conflict(open_conflict["conflict_id"], resolution="同一回执已释放资金，拒绝二次验收", actor="person:auditor")
    app.workflows.request_carryover(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
        {"topic_id": topic_a["unit_id"], "source_kind": "carried", "source_ref": "carry-2028-PRJ-ALLOY", "amount": "200000.00"},
        {"topic_id": topic_a["unit_id"], "source_kind": "provincial", "source_ref": "PROV-2028-01", "amount": "500000.00"},
        {"topic_id": topic_b["unit_id"], "source_kind": "carried", "source_ref": "carry-2028-PRJ-ALLOY", "amount": "100000.00"},
        {"topic_id": topic_b["unit_id"], "source_kind": "enterprise", "source_ref": "FIRM-MATCH-10", "amount": "300000.00"},
    ], actor="demo-operator", request_key="carry-2028-PRJ-ALLOY")
    carried = app.workflows.run_due()
    carried_replay = app.workflows.run_due()
    drill = app.portfolio.drill_down(contract["contract_id"])
    return {
        "guideline": guide,
        "hierarchy": {"plan": plan["unit_id"], "program": program["unit_id"], "project": project["unit_id"], "topics": [topic_a["unit_id"], topic_b["unit_id"]]},
        "budget": budget,
        "topic_a_status": book.topic_budget_status(topic_a["unit_id"]),
        "topic_b_status": book.topic_budget_status(topic_b["unit_id"]),
        "facility": {"usage": usage["usage_id"], "charge_entry": charged["entry_id"]},
        "material_view_for_partner": book.view_material(material_id=material["material_id"], org_id="org:firm"),
        "ip": ip["ip_id"],
        "contract": contract["contract_id"],
        "contributions": contributions,
        "self_approval_blocked": own_review,
        "adjustment_applied": applied,
        "acceptance": {"first": first_release, "replay": replay_release, "duplicate_blocked": duplicate_receipt},
        "carryover_job": carried,
        "carryover_replay_empty": carried_replay,
        "drill_down": drill,
        "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("portfolio-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "portfolio-demo": emit(portfolio_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
