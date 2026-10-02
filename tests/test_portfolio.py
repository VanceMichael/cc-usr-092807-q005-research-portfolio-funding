from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.organizations import OrganizationService
from civicflow.portfolio import (
    ADJUSTMENTS,
    MILESTONES,
    PROJECTS,
    TASKS,
)
from civicflow.portfolio_funds import ACCEPTANCES, journal_key
from civicflow.security import AccessContext


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-09-28T12:00:00+08:00")
        self.system = AccessContext.system("tester")
        self.approver = AccessContext(actor_id="person:boss", permissions=frozenset({"approve:portfolio", "read:portfolio"}))

    def tearDown(self):
        self.temp.cleanup()

    # ---- 测试辅助 ----

    def make_structure(self, *, initiate: bool = True) -> dict:
        portfolio = self.app.portfolio
        organizations = OrganizationService(self.app.repository)
        lead = organizations.create(self.system, {"name": "牵头单位", "kind": "科研院所", "jurisdiction": "本省", "owner_id": "person:dean"}, request_key="org-lead")
        partner = organizations.create(self.system, {"name": "合作企业", "kind": "企业", "jurisdiction": "本省", "owner_id": "person:ceo"}, request_key="org-partner")
        program = portfolio.create_program(self.system, {"code": "KJ-2026", "title": "重点研发计划", "guideline_ref": "指南:2026", "guideline_version": "v1", "owner_org": lead["entity_id"]}, request_key="program")
        program = portfolio.publish_program(self.system, program["entity_id"], expected_version=program["version"], request_key="program-publish")
        topic = portfolio.create_topic(self.system, {"program_id": program["entity_id"], "code": "ZT-1", "title": "专题一"}, request_key="topic")
        project = portfolio.create_project(self.system, {"topic_id": topic["entity_id"], "code": "XM-1", "title": "项目一", "lead_org": lead["entity_id"], "partner_orgs": [partner["entity_id"]], "pi_id": "person:pi"}, request_key="project")
        if initiate:
            project = portfolio.initiate_project(self.system, project["entity_id"], {"budget_version": "2026-v1"}, expected_version=project["version"], request_key="project-init")
        task_a = portfolio.create_task(self.system, {"project_id": project["entity_id"], "code": "KT-A", "title": "课题甲", "lead_org": lead["entity_id"]}, request_key="task-a")
        task_b = portfolio.create_task(self.system, {"project_id": project["entity_id"], "code": "KT-B", "title": "课题乙", "lead_org": partner["entity_id"]}, request_key="task-b")
        ms_a1 = portfolio.create_milestone(self.system, {"task_id": task_a["entity_id"], "seq": 1, "title": "甲-1", "due_at": "2026-06-30T00:00:00+08:00", "budget": "100.00", "owner_id": "person:pi"}, request_key="ms-a1")
        ms_a2 = portfolio.create_milestone(self.system, {"task_id": task_a["entity_id"], "seq": 2, "title": "甲-2", "due_at": "2026-09-30T00:00:00+08:00", "budget": "200.00", "owner_id": "person:pi"}, request_key="ms-a2")
        ms_b1 = portfolio.create_milestone(self.system, {"task_id": task_b["entity_id"], "seq": 1, "title": "乙-1", "due_at": "2026-12-31T00:00:00+08:00", "budget": "300.00", "owner_id": "person:pi"}, request_key="ms-b1")
        return {"lead": lead, "partner": partner, "program": program, "topic": topic, "project": project, "task_a": task_a, "task_b": task_b, "ms_a1": ms_a1, "ms_a2": ms_a2, "ms_b1": ms_b1}

    def approve_and_apply(self, adjustment: dict, *, key: str) -> dict:
        portfolio = self.app.portfolio
        portfolio.approve_adjustment(self.approver, adjustment["entity_id"], {"approve": True, "reason": "同意"}, expected_version=adjustment["version"], request_key=f"{key}-approve")
        return portfolio.apply_adjustment(self.system, adjustment["entity_id"], request_key=f"{key}-apply")

    # ---- 结构关系 ----

    def test_structure_requires_existing_parents(self):
        portfolio = self.app.portfolio
        with self.assertRaises(NotFoundError):
            portfolio.create_topic(self.system, {"program_id": "programs:missing", "code": "ZT", "title": "专题"}, request_key="bad-topic")
        structure = self.make_structure()
        with self.assertRaises(NotFoundError):
            portfolio.create_task(self.system, {"project_id": "projects:missing", "code": "KT", "title": "课题", "lead_org": structure["lead"]["entity_id"]}, request_key="bad-task")
        with self.assertRaises(NotFoundError):
            portfolio.create_milestone(self.system, {"task_id": "tasks:missing", "seq": 1, "title": "里程碑", "due_at": "2026-01-01T00:00:00+08:00", "budget": "1", "owner_id": "person:x"}, request_key="bad-ms")
        with self.assertRaises(NotFoundError):
            portfolio.create_project(self.system, {"topic_id": structure["topic"]["entity_id"], "code": "XM-2", "title": "项目二", "lead_org": "organizations:missing", "partner_orgs": [], "pi_id": "person:pi"}, request_key="bad-project")

    def test_contract_allocation_bounded_by_amount(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        with self.assertRaises(ValidationError):
            portfolio.create_contract(self.system, {"code": "HT-1", "title": "合同", "buyer_org": "org:buyer", "amount": "100.00", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": structure["task_a"]["entity_id"], "amount": "120.00"}]}, request_key="contract-over")
        contract = portfolio.create_contract(self.system, {"code": "HT-1", "title": "合同", "buyer_org": "org:buyer", "amount": "100.00", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": structure["task_a"]["entity_id"], "amount": "60.00"}]}, request_key="contract-ok")
        with self.assertRaises(ValidationError):
            portfolio.allocate_contract(self.system, contract["entity_id"], [{"task_id": structure["task_b"]["entity_id"], "amount": "50.00"}], expected_version=contract["version"], request_key="alloc-over")
        updated = portfolio.allocate_contract(self.system, contract["entity_id"], [{"task_id": structure["task_b"]["entity_id"], "amount": "40.00"}], expected_version=contract["version"], request_key="alloc-ok")
        self.assertEqual(sum(item["amount_minor"] for item in updated["allocations"]), 10000)

    def test_initiate_requires_published_program(self):
        portfolio = self.app.portfolio
        organizations = OrganizationService(self.app.repository)
        lead = organizations.create(self.system, {"name": "单位", "kind": "院所", "jurisdiction": "本省", "owner_id": "person:d"}, request_key="org-only")
        program = portfolio.create_program(self.system, {"code": "KJ", "title": "计划", "guideline_ref": "指南", "guideline_version": "v1", "owner_org": lead["entity_id"]}, request_key="program-draft")
        topic = portfolio.create_topic(self.system, {"program_id": program["entity_id"], "code": "ZT", "title": "专题"}, request_key="topic-draft")
        project = portfolio.create_project(self.system, {"topic_id": topic["entity_id"], "code": "XM", "title": "项目", "lead_org": lead["entity_id"], "partner_orgs": [], "pi_id": "person:pi"}, request_key="project-draft")
        with self.assertRaises(ConflictError):
            portfolio.initiate_project(self.system, project["entity_id"], {"budget_version": "v1"}, expected_version=project["version"], request_key="init-early")

    # ---- 冻结 ----

    def test_initiation_freezes_guideline_and_budget(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        project = structure["project"]
        self.assertEqual(project["frozen_guideline"]["version"], "v1")
        self.assertEqual(project["frozen_budget_version"], "2026-v1")
        program = portfolio.update_program_guideline(self.system, structure["program"]["entity_id"], {"guideline_ref": "指南:2026", "guideline_version": "v2"}, expected_version=structure["program"]["version"], request_key="guideline-v2")
        self.assertEqual(program["guideline_version"], "v2")
        reloaded = portfolio.get(self.system, PROJECTS, project["entity_id"])
        self.assertEqual(reloaded["frozen_guideline"]["version"], "v1")
        budget = portfolio.submit_adjustment(self.system, {"project_id": project["entity_id"], "kind": "budget", "detail": {"new_budget_version": "2026-v2"}}, request_key="adj-budget")
        self.approve_and_apply(budget, key="adj-budget")
        reloaded = portfolio.get(self.system, PROJECTS, project["entity_id"])
        self.assertEqual(reloaded["frozen_budget_version"], "2026-v2")
        self.assertEqual(reloaded["previous_budget_version"], "2026-v1")
        guideline = portfolio.submit_adjustment(self.system, {"project_id": project["entity_id"], "kind": "guideline", "detail": {}}, request_key="adj-guideline")
        self.approve_and_apply(guideline, key="adj-guideline")
        reloaded = portfolio.get(self.system, PROJECTS, project["entity_id"])
        self.assertEqual(reloaded["frozen_guideline"]["version"], "v2")
        history = self.app.repository.history(PROJECTS, project["entity_id"])
        frozen_versions = [row.get("frozen_budget_version") for row in history]
        self.assertIn("2026-v1", frozen_versions)
        self.assertIn("2026-v2", frozen_versions)

    # ---- 资金分录 ----

    def test_fund_entries_immutable_and_guarded(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        project_id = structure["project"]["entity_id"]
        funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "国家任务", "amount": "1000.00", "reference": "ap-1"})
        funds.post_matching(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "500.00", "reference": "mt-1"})
        funds.post_expenditure(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "200.00", "reference": "ex-1"})
        funds.post_refund(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "50.00", "reference": "rf-1"})
        journal = journal_key(project_id, 2026)
        self.assertEqual(self.app.ledger.balance(journal, currency="CNY"), 135000)
        with self.assertRaises(ConflictError):
            funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "国家任务", "amount": "10.00", "reference": "ap-1"})
        with self.assertRaises(ValidationError):
            funds.post_expenditure(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "2000.00", "reference": "ex-2"})
        with self.assertRaises(ValidationError):
            funds.post_refund(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "300.00", "reference": "rf-2"})
        entries = self.app.ledger.entries(journal)
        self.assertEqual(len(entries), 4)
        self.assertEqual({entry["posted_by"] for entry in entries}, {"tester"})
        with self.assertRaises(ValidationError):
            funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "未知渠道", "amount": "1.00", "reference": "ap-2"})

    # ---- 重大调整只影响未完成阶段 ----

    def test_delay_only_touches_pending_milestones(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        portfolio.complete_milestone(self.system, structure["ms_a1"]["entity_id"], expected_version=structure["ms_a1"]["version"], request_key="ms-a1-done")
        done = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "delay", "detail": {"milestone_id": structure["ms_a1"]["entity_id"], "new_due_at": "2027-01-31T00:00:00+08:00"}}, request_key="adj-delay-done")
        portfolio.approve_adjustment(self.approver, done["entity_id"], {"approve": True, "reason": "同意"}, expected_version=done["version"], request_key="adj-delay-done-approve")
        with self.assertRaises(ConflictError):
            portfolio.apply_adjustment(self.system, done["entity_id"], request_key="adj-delay-done-apply")
        pending = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "delay", "detail": {"milestone_id": structure["ms_a2"]["entity_id"], "new_due_at": "2027-01-31T00:00:00+08:00"}}, request_key="adj-delay-pending")
        self.approve_and_apply(pending, key="adj-delay-pending")
        moved = portfolio.get(self.system, MILESTONES, structure["ms_a2"]["entity_id"])
        self.assertEqual(moved["due_at"], "2027-01-30T16:00:00Z")
        untouched = portfolio.get(self.system, MILESTONES, structure["ms_a1"]["entity_id"])
        self.assertEqual(untouched["due_at"], structure["ms_a1"]["due_at"])

    def test_pi_change_only_touches_pending_milestones(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        portfolio.complete_milestone(self.system, structure["ms_a1"]["entity_id"], expected_version=structure["ms_a1"]["version"], request_key="ms-a1-done")
        adjustment = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "pi_change", "detail": {"new_pi_id": "person:newpi"}}, request_key="adj-pi")
        self.approve_and_apply(adjustment, key="adj-pi")
        project = portfolio.get(self.system, PROJECTS, structure["project"]["entity_id"])
        self.assertEqual(project["pi_id"], "person:newpi")
        self.assertEqual(project["previous_pi_id"], "person:pi")
        completed = portfolio.get(self.system, MILESTONES, structure["ms_a1"]["entity_id"])
        self.assertEqual(completed["owner_id"], "person:pi")
        for key in ("ms_a2", "ms_b1"):
            milestone = portfolio.get(self.system, MILESTONES, structure[key]["entity_id"])
            self.assertEqual(milestone["owner_id"], "person:newpi")

    def test_merge_moves_only_pending_milestones(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        portfolio.complete_milestone(self.system, structure["ms_a1"]["entity_id"], expected_version=structure["ms_a1"]["version"], request_key="ms-a1-done")
        adjustment = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "merge", "detail": {"source_task_id": structure["task_a"]["entity_id"], "target_task_id": structure["task_b"]["entity_id"]}}, request_key="adj-merge")
        self.approve_and_apply(adjustment, key="adj-merge")
        moved = portfolio.get(self.system, MILESTONES, structure["ms_a2"]["entity_id"])
        self.assertEqual(moved["task_id"], structure["task_b"]["entity_id"])
        self.assertEqual(moved["merged_from"], structure["task_a"]["entity_id"])
        stayed = portfolio.get(self.system, MILESTONES, structure["ms_a1"]["entity_id"])
        self.assertEqual(stayed["task_id"], structure["task_a"]["entity_id"])
        source = portfolio.get(self.system, TASKS, structure["task_a"]["entity_id"])
        self.assertEqual(source["state"], "merged")
        self.assertEqual(source["merged_into"], structure["task_b"]["entity_id"])
        replay = portfolio.apply_adjustment(self.system, adjustment["entity_id"], request_key="adj-merge-apply-again")
        self.assertEqual(replay["state"], "applied")
        self.assertEqual(portfolio.get(self.system, MILESTONES, structure["ms_a2"]["entity_id"])["task_id"], structure["task_b"]["entity_id"])

    def test_adjustment_requires_approval(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        adjustment = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "budget", "detail": {"new_budget_version": "2026-v2"}}, request_key="adj-unapproved")
        with self.assertRaises(ConflictError):
            portfolio.apply_adjustment(self.system, adjustment["entity_id"], request_key="adj-unapproved-apply")

    def test_pi_cannot_approve_own_adjustment(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        pi = AccessContext(actor_id="person:pi", permissions=frozenset({"write:portfolio", "approve:portfolio"}), scopes=frozenset({f"org:{structure['lead']['entity_id']}"}))
        adjustment = portfolio.submit_adjustment(self.system, {"project_id": structure["project"]["entity_id"], "kind": "budget", "detail": {"new_budget_version": "2026-v2"}}, request_key="adj-self")
        with self.assertRaises(PermissionDenied):
            portfolio.approve_adjustment(pi, adjustment["entity_id"], {"approve": True, "reason": "自审"}, expected_version=adjustment["version"], request_key="adj-self-approve")
        with self.assertRaises(PermissionDenied):
            portfolio.approve_adjustment(self.system, adjustment["entity_id"], {"approve": True, "reason": "提交人自审"}, expected_version=adjustment["version"], request_key="adj-self-approve-2")
        approved = portfolio.approve_adjustment(self.approver, adjustment["entity_id"], {"approve": True, "reason": "同意"}, expected_version=adjustment["version"], request_key="adj-other-approve")
        self.assertEqual(approved["state"], "approved")

    # ---- 设备占用 ----

    def test_restricted_equipment_cannot_overlap(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        equipment = portfolio.create_equipment(self.system, {"name": "电子显微镜", "kind": "大型设备", "owner_org": structure["lead"]["entity_id"]}, request_key="equip-1")
        portfolio.book_equipment(self.system, equipment["entity_id"], structure["task_a"]["entity_id"], start_at="2026-10-10T09:00:00+08:00", end_at="2026-10-10T10:00:00+08:00")
        with self.assertRaises(ConflictError):
            portfolio.book_equipment(self.system, equipment["entity_id"], structure["task_b"]["entity_id"], start_at="2026-10-10T09:30:00+08:00", end_at="2026-10-10T10:30:00+08:00")
        portfolio.book_equipment(self.system, equipment["entity_id"], structure["task_b"]["entity_id"], start_at="2026-10-10T10:00:00+08:00", end_at="2026-10-10T11:00:00+08:00")
        sample = portfolio.create_equipment(self.system, {"name": "受限样品", "kind": "受限样品", "owner_org": structure["lead"]["entity_id"]}, request_key="equip-2")
        portfolio.book_equipment(self.system, sample["entity_id"], structure["task_a"]["entity_id"], start_at="2026-10-10T09:00:00+08:00", end_at="2026-10-12T09:00:00+08:00")
        with self.assertRaises(ConflictError):
            portfolio.book_equipment(self.system, sample["entity_id"], structure["task_b"]["entity_id"], start_at="2026-10-11T09:00:00+08:00", end_at="2026-10-13T09:00:00+08:00")
        with self.assertRaises(ValidationError):
            portfolio.create_equipment(self.system, {"name": "违规设备", "kind": "大型设备", "owner_org": structure["lead"]["entity_id"], "capacity": 2}, request_key="equip-3")

    # ---- 成果转化 ----

    def test_transfer_requires_full_contribution(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        contract = portfolio.create_contract(self.system, {"code": "HT-1", "title": "合同", "buyer_org": "org:buyer", "amount": "600.00", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": structure["task_a"]["entity_id"], "amount": "600.00"}]}, request_key="contract")
        with self.assertRaises(ValidationError):
            portfolio.record_transfer(self.system, {"contract_id": contract["entity_id"], "amount": "600.00", "fiscal_year": 2027, "contributions": [{"project_id": structure["project"]["entity_id"], "ratio": "0.9"}]}, request_key="transfer-bad")
        with self.assertRaises(ValidationError):
            portfolio.record_transfer(self.system, {"contract_id": contract["entity_id"], "amount": "600.00", "fiscal_year": 2027, "contributions": [{"project_id": structure["project"]["entity_id"], "ratio": "0.5"}, {"project_id": structure["project"]["entity_id"], "ratio": "0.5"}]}, request_key="transfer-dup")
        transfer = portfolio.record_transfer(self.system, {"contract_id": contract["entity_id"], "amount": "600.00", "fiscal_year": 2027, "contributions": [{"project_id": structure["project"]["entity_id"], "ratio": "1"}]}, request_key="transfer-ok")
        self.assertEqual(transfer["contributions"][0]["ratio"], "1")

    def test_transfer_income_distributed_by_ratio(self):
        portfolio = self.app.portfolio
        funds = self.app.portfolio_funds
        structure = self.make_structure()
        other = self.make_second_project(structure)
        contract = portfolio.create_contract(self.system, {"code": "HT-2", "title": "合同", "buyer_org": "org:buyer", "amount": "900.00", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": structure["task_a"]["entity_id"], "amount": "900.00"}]}, request_key="contract-2")
        transfer = portfolio.record_transfer(self.system, {"contract_id": contract["entity_id"], "amount": "900.00", "fiscal_year": 2027, "contributions": [{"project_id": structure["project"]["entity_id"], "ratio": "0.5"}, {"project_id": other["entity_id"], "ratio": "0.5"}]}, request_key="transfer-2")
        result = funds.distribute_transfer_income(self.system, transfer["entity_id"])
        self.assertEqual(sum(share["amount_minor"] for share in result["shares"]), 90000)
        replay = funds.distribute_transfer_income(self.system, transfer["entity_id"])
        self.assertTrue(all(share.get("replayed") for share in replay["shares"]))
        self.assertEqual(self.app.ledger.balance(journal_key(structure["project"]["entity_id"], 2027), currency="CNY"), 45000)
        self.assertEqual(self.app.ledger.balance(journal_key(other["entity_id"], 2027), currency="CNY"), 45000)

    def make_second_project(self, structure: dict) -> dict:
        portfolio = self.app.portfolio
        topic_b = portfolio.create_topic(self.system, {"program_id": structure["program"]["entity_id"], "code": "ZT-2", "title": "专题二"}, request_key="topic-2")
        project = portfolio.create_project(self.system, {"topic_id": topic_b["entity_id"], "code": "XM-2", "title": "项目二", "lead_org": structure["lead"]["entity_id"], "partner_orgs": [], "pi_id": "person:other"}, request_key="project-2")
        return portfolio.initiate_project(self.system, project["entity_id"], {"budget_version": "2026-v1"}, expected_version=project["version"], request_key="project-2-init")

    # ---- 资料授权 ----

    def test_collaborator_only_sees_authorized_materials(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        project_id = structure["project"]["entity_id"]
        material = portfolio.share_material(self.system, {"project_id": project_id, "title": "工艺图纸", "kind": "图纸", "allowed_orgs": [structure["partner"]["entity_id"]], "content_ref": "doc:1"}, request_key="mat-1")
        partner = AccessContext(actor_id="person:partner", permissions=frozenset({"read:portfolio"}), scopes=frozenset({f"org:{structure['partner']['entity_id']}"}))
        outsider = AccessContext(actor_id="person:outsider", permissions=frozenset({"read:portfolio"}), scopes=frozenset({"org:organizations:elsewhere"}))
        self.assertEqual(portfolio.get_material(partner, material["entity_id"])["title"], "工艺图纸")
        self.assertEqual(len(portfolio.list_materials(partner, project_id)), 1)
        with self.assertRaises(PermissionDenied):
            portfolio.get_material(outsider, material["entity_id"])
        with self.assertRaises(PermissionDenied):
            portfolio.list_materials(outsider, project_id)
        lead_only = AccessContext(actor_id="person:lead", permissions=frozenset({"read:portfolio"}), scopes=frozenset({f"org:{structure['lead']['entity_id']}"}))
        self.assertEqual(portfolio.list_materials(lead_only, project_id), [])

    # ---- 验收回执 ----

    def acceptance_payload(self, structure: dict, *, amount_a: str = "100.00", amount_b: str = "200.00") -> dict:
        return {"project_id": structure["project"]["entity_id"], "fiscal_year": 2026, "result": "通过", "releases": [{"milestone_id": structure["ms_a1"]["entity_id"], "amount": amount_a}, {"milestone_id": structure["ms_a2"]["entity_id"], "amount": amount_b}]}

    def test_duplicate_acceptance_does_not_release_twice(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        receipt = {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure)}
        first = funds.receive_acceptance(self.system, receipt)
        self.assertEqual(first["status"], "accepted")
        results = funds.run_due_jobs(self.system)
        self.assertEqual([item["status"] for item in results], ["succeeded"])
        journal = journal_key(structure["project"]["entity_id"], 2026)
        self.assertEqual(self.app.ledger.balance(journal, currency="CNY"), 30000)
        second = funds.receive_acceptance(self.system, receipt)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["acceptance_id"], first["acceptance_id"])
        self.assertEqual(funds.run_due_jobs(self.system), [])
        self.assertEqual(self.app.ledger.balance(journal, currency="CNY"), 30000)
        acceptance = self.app.repository.get(ACCEPTANCES, first["acceptance_id"])
        self.assertEqual(acceptance["state"], "settled")

    def test_conflicting_receipt_goes_to_review(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        portfolio = self.app.portfolio
        funds.receive_acceptance(self.system, {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure)})
        conflict = funds.receive_acceptance(self.system, {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure, amount_a="150.00")})
        self.assertEqual(conflict["status"], "review")
        self.assertEqual(self.app.verify()["inbox_conflicts"], 1)
        again = funds.receive_acceptance(self.system, {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure, amount_a="150.00")})
        self.assertEqual(again["review_id"], conflict["review_id"])
        reviews = portfolio.list_reviews(self.system, state="pending")
        self.assertEqual(len(reviews), 1)
        resolved = portfolio.resolve_review(self.approver, reviews[0]["entity_id"], {"resolution": "以首次回执为准"}, expected_version=reviews[0]["version"], request_key="resolve-1")
        self.assertEqual(resolved["state"], "resolved")
        reader = AccessContext(actor_id="person:reader", permissions=frozenset({"read:portfolio"}))
        with self.assertRaises(PermissionDenied):
            portfolio.list_reviews(reader)

    # ---- 中断接续 ----

    def test_settlement_resumes_after_interruption(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        acceptance = funds.receive_acceptance(self.system, {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure)})
        journal = journal_key(structure["project"]["entity_id"], 2026)
        # 模拟中断:第一笔释放分录已入账,结算任务尚未完成
        self.app.ledger.post(journal_key=journal, account="release", currency="CNY", amount="100.00", direction="debit", reference=f"accept:{acceptance['acceptance_id']}:0", actor="tester")
        results = funds.run_due_jobs(self.system)
        self.assertEqual([item["status"] for item in results], ["succeeded"])
        self.assertEqual(self.app.ledger.balance(journal, currency="CNY"), 30000)
        releases = [entry for entry in self.app.ledger.entries(journal) if entry["account"] == "release"]
        self.assertEqual(len(releases), 2)
        self.assertEqual(self.app.repository.get(ACCEPTANCES, acceptance["acceptance_id"])["state"], "settled")

    def test_stale_job_lease_is_reclaimed(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        project_id = structure["project"]["entity_id"]
        funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "省级专项", "amount": "100.00", "reference": "ap-1"})
        funds.schedule_carryover(self.system, {"project_id": project_id, "from_year": 2026, "to_year": 2027})
        claimed = self.app.jobs.claim_due()
        self.assertEqual(len(claimed), 1)  # 执行者拿到租约后崩溃,任务停留在 running
        later = CivicFlow.open(self.db_path, fixed_now="2026-09-28T12:00:31+08:00")
        results = later.portfolio_funds.run_due_jobs(self.system)
        self.assertEqual([item["status"] for item in results], ["succeeded"])
        self.assertEqual(self.app.ledger.balance(journal_key(project_id, 2026), currency="CNY"), 0)
        self.assertEqual(self.app.ledger.balance(journal_key(project_id, 2027), currency="CNY"), 10000)

    def test_carryover_resumes_midway_and_stays_idempotent(self):
        structure = self.make_structure()
        funds = self.app.portfolio_funds
        project_id = structure["project"]["entity_id"]
        funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "国家任务", "amount": "100.00", "reference": "ap-1"})
        funds.post_expenditure(self.system, {"project_id": project_id, "fiscal_year": 2026, "amount": "30.00", "reference": "ex-1"})
        scheduled = funds.schedule_carryover(self.system, {"project_id": project_id, "from_year": 2026, "to_year": 2027})
        replay = funds.schedule_carryover(self.system, {"project_id": project_id, "from_year": 2026, "to_year": 2027})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job_id"], scheduled["job_id"])
        # 模拟中断:转出分录已入账,转入分录尚未发生
        self.app.ledger.post(journal_key=journal_key(project_id, 2026), account="carryout", currency="CNY", amount="70.00", direction="credit", reference=f"carryover:{project_id}:2026:out", actor="tester")
        results = funds.run_due_jobs(self.system)
        self.assertEqual([item["status"] for item in results], ["succeeded"])
        self.assertEqual(self.app.ledger.balance(journal_key(project_id, 2026), currency="CNY"), 0)
        self.assertEqual(self.app.ledger.balance(journal_key(project_id, 2027), currency="CNY"), 7000)
        funds._carryover({"project_id": project_id, "from_year": 2026, "to_year": 2027, "currency": "CNY", "actor": "tester"})
        carried_in = [entry for entry in self.app.ledger.entries(journal_key(project_id, 2027)) if entry["account"] == "carryin"]
        self.assertEqual(len(carried_in), 1)
        self.assertEqual(self.app.ledger.balance(journal_key(project_id, 2027), currency="CNY"), 7000)

    # ---- 审核下钻 ----

    def test_contract_drilldown_reaches_entries_usage_and_owners(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        funds = self.app.portfolio_funds
        project_id = structure["project"]["entity_id"]
        portfolio.create_budget_source(self.system, {"project_id": project_id, "channel": "国家任务", "fiscal_year": 2026, "amount": "1000.00"}, request_key="bs-1")
        funds.post_appropriation(self.system, {"project_id": project_id, "fiscal_year": 2026, "channel": "国家任务", "amount": "1000.00", "reference": "ap-1"})
        equipment = portfolio.create_equipment(self.system, {"name": "测量机", "kind": "大型设备", "owner_org": structure["lead"]["entity_id"]}, request_key="equip")
        portfolio.book_equipment(self.system, equipment["entity_id"], structure["task_a"]["entity_id"], start_at="2026-10-10T09:00:00+08:00", end_at="2026-10-10T10:00:00+08:00")
        portfolio.create_ip(self.system, {"project_id": project_id, "kind": "发明专利", "title": "装配方法", "registration_ref": "ZL-1"}, request_key="ip-1")
        contract = portfolio.create_contract(self.system, {"code": "HT-1", "title": "技术合同", "buyer_org": "org:buyer", "amount": "600.00", "signed_at": "2026-09-01T00:00:00+08:00", "allocations": [{"task_id": structure["task_a"]["entity_id"], "amount": "400.00"}, {"task_id": structure["task_b"]["entity_id"], "amount": "150.00"}]}, request_key="contract")
        auditor = AccessContext(actor_id="person:auditor", permissions=frozenset({"audit:portfolio", "read:portfolio"}))
        report = funds.contract_drilldown(auditor, contract["entity_id"])
        self.assertEqual(report["allocated_minor"], 55000)
        self.assertEqual(report["unallocated_minor"], 5000)
        self.assertEqual(len(report["tasks"]), 2)
        project_section = report["projects"][0]
        self.assertEqual(project_section["pi_id"], "person:pi")
        self.assertEqual(project_section["frozen_budget_version"], "2026-v1")
        entries = [entry for journal in project_section["journals"] for entry in journal["entries"]]
        self.assertTrue(any(entry["reference"] == "ap-1" and entry["posted_by"] == "tester" for entry in entries))
        self.assertEqual(project_section["equipment_usage"][0]["created_by"], "tester")
        self.assertEqual(project_section["equipment_usage"][0]["equipment_name"], "测量机")
        self.assertEqual(project_section["pending_milestone_budget_minor"], 60000)
        self.assertEqual(len(project_section["budget_sources"]), 1)
        view = funds.project_budget_view(auditor, project_id)
        self.assertEqual(view["pending_milestone_budget_minor"], 60000)
        reader = AccessContext(actor_id="person:reader", permissions=frozenset({"read:portfolio"}))
        with self.assertRaises(PermissionDenied):
            funds.contract_drilldown(reader, contract["entity_id"])

    def test_project_accepted_after_all_milestones_settled(self):
        structure = self.make_structure()
        portfolio = self.app.portfolio
        funds = self.app.portfolio_funds
        for key in ("ms_a1", "ms_a2", "ms_b1"):
            portfolio.complete_milestone(self.system, structure[key]["entity_id"], expected_version=structure[key]["version"], request_key=f"{key}-done")
        funds.receive_acceptance(self.system, {"source": "acc-port", "source_key": "contract-1", "sequence": 1, "payload": self.acceptance_payload(structure)})
        funds.run_due_jobs(self.system)
        project = portfolio.get(self.system, PROJECTS, structure["project"]["entity_id"])
        self.assertEqual(project["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
