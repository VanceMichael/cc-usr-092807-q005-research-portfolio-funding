"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .organizations import OrganizationService
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
    """研发组合管理端到端演示:立项冻结、资金分录、验收结算、预算结转与合同下钻。"""
    context = AccessContext.system("portfolio-demo")
    portfolio, funds = app.portfolio, app.portfolio_funds
    organizations = OrganizationService(app.repository)
    lead = organizations.create(context, {"name": "省科学院", "kind": "科研院所", "jurisdiction": "本省", "owner_id": "person:dean"}, request_key="demo-org-lead")
    partner = organizations.create(context, {"name": "联合企业", "kind": "企业", "jurisdiction": "本省", "owner_id": "person:ceo"}, request_key="demo-org-partner")
    program = portfolio.create_program(context, {"code": "KJ-2026", "title": "省重点研发计划", "guideline_ref": "指南:2026", "guideline_version": "v1", "owner_org": lead["entity_id"]}, request_key="demo-program")
    program = portfolio.publish_program(context, program["entity_id"], expected_version=program["version"], request_key="demo-program-publish")
    topic = portfolio.create_topic(context, {"program_id": program["entity_id"], "code": "KJ-2026-ZT1", "title": "智能制造专题"}, request_key="demo-topic")
    project = portfolio.create_project(context, {"topic_id": topic["entity_id"], "code": "KJ-2026-ZT1-XM1", "title": "高精度装配项目", "lead_org": lead["entity_id"], "partner_orgs": [partner["entity_id"]], "pi_id": "person:pi"}, request_key="demo-project")
    project = portfolio.initiate_project(context, project["entity_id"], {"budget_version": "2026-预算-v1"}, expected_version=project["version"], request_key="demo-project-initiate")
    task_a = portfolio.create_task(context, {"project_id": project["entity_id"], "code": "KT-01", "title": "装配工艺课题", "lead_org": lead["entity_id"]}, request_key="demo-task-a")
    task_b = portfolio.create_task(context, {"project_id": project["entity_id"], "code": "KT-02", "title": "检测平台课题", "lead_org": partner["entity_id"]}, request_key="demo-task-b")
    milestone_a = portfolio.create_milestone(context, {"task_id": task_a["entity_id"], "seq": 1, "title": "工艺验证", "due_at": "2026-06-30T00:00:00+08:00", "budget": "50000", "owner_id": "person:pi"}, request_key="demo-ms-a")
    portfolio.create_milestone(context, {"task_id": task_b["entity_id"], "seq": 1, "title": "平台联调", "due_at": "2026-12-31T00:00:00+08:00", "budget": "80000", "owner_id": "person:pi"}, request_key="demo-ms-b")
    portfolio.create_budget_source(context, {"project_id": project["entity_id"], "channel": "国家任务", "fiscal_year": 2026, "amount": "800000"}, request_key="demo-bs-1")
    portfolio.create_budget_source(context, {"project_id": project["entity_id"], "channel": "企业合作", "fiscal_year": 2026, "amount": "200000"}, request_key="demo-bs-2")
    funds.post_appropriation(context, {"project_id": project["entity_id"], "fiscal_year": 2026, "channel": "国家任务", "amount": "800000", "reference": "拨款-2026-001"})
    funds.post_matching(context, {"project_id": project["entity_id"], "fiscal_year": 2026, "amount": "200000", "reference": "配套-2026-001"})
    funds.post_expenditure(context, {"project_id": project["entity_id"], "fiscal_year": 2026, "amount": "120000", "reference": "支出-2026-001"})
    funds.post_refund(context, {"project_id": project["entity_id"], "fiscal_year": 2026, "amount": "5000", "reference": "退回-2026-001"})
    equipment = portfolio.create_equipment(context, {"name": "三坐标测量机", "kind": "大型设备", "owner_org": lead["entity_id"]}, request_key="demo-equipment")
    booking_a = portfolio.book_equipment(context, equipment["entity_id"], task_a["entity_id"], start_at="2026-10-10T09:00:00+08:00", end_at="2026-10-10T11:00:00+08:00")
    portfolio.book_equipment(context, equipment["entity_id"], task_b["entity_id"], start_at="2026-10-10T11:00:00+08:00", end_at="2026-10-10T12:00:00+08:00")
    portfolio.complete_milestone(context, milestone_a["entity_id"], expected_version=milestone_a["version"], request_key="demo-ms-a-done")
    contract = portfolio.create_contract(context, {"code": "HT-2026-01", "title": "装配技术转让合同", "buyer_org": "org:buyer", "amount": "600000", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": task_a["entity_id"], "amount": "360000"}, {"task_id": task_b["entity_id"], "amount": "240000"}]}, request_key="demo-contract")
    receipt = {"project_id": project["entity_id"], "fiscal_year": 2026, "result": "通过", "releases": [{"milestone_id": milestone_a["entity_id"], "amount": "50000"}]}
    acceptance = funds.receive_acceptance(context, {"source": "acceptance-port", "source_key": contract["entity_id"], "sequence": 1, "payload": receipt, "occurred_at": app.clock.now()})
    settlement = funds.run_due_jobs(context)
    duplicate = funds.receive_acceptance(context, {"source": "acceptance-port", "source_key": contract["entity_id"], "sequence": 1, "payload": receipt, "occurred_at": app.clock.now()})
    carryover = funds.schedule_carryover(context, {"project_id": project["entity_id"], "from_year": 2026, "to_year": 2027})
    funds.run_due_jobs(context)
    portfolio.create_ip(context, {"project_id": project["entity_id"], "kind": "发明专利", "title": "高精度装配方法", "registration_ref": "ZL-2026-001"}, request_key="demo-ip")
    transfer = portfolio.record_transfer(context, {"contract_id": contract["entity_id"], "amount": "600000", "fiscal_year": 2027, "contributions": [{"project_id": project["entity_id"], "ratio": "1"}]}, request_key="demo-transfer")
    funds.distribute_transfer_income(context, transfer["entity_id"])
    drilldown = funds.contract_drilldown(context, contract["entity_id"])
    return {
        "project": {"entity_id": project["entity_id"], "state": project["state"], "frozen_guideline": project["frozen_guideline"], "frozen_budget_version": project["frozen_budget_version"]},
        "booking": booking_a,
        "acceptance": acceptance,
        "settlement": settlement,
        "duplicate_receipt": duplicate,
        "carryover": carryover,
        "budget_view": funds.project_budget_view(context, project["entity_id"]),
        "drilldown": drilldown,
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
