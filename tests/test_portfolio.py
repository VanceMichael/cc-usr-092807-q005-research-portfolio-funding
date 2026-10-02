from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.timeutil import parse_instant


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "pf.sqlite3")
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-10-02T09:00:00+08:00")
        self.book = self.app.portfolio

    def tearDown(self):
        self.temp.cleanup()

    # ----- 夹具 -----

    def _stack(self, *, budget_a="1000000.00", budget_b="500000.00"):
        self.book.freeze_guideline(guideline_id="guide:g", title="指南", content={"v": 1}, actor="admin")
        plan = self.book.create_unit(unit_type="plan", code="P", name="计划", guideline=("guide:g", 1), actor="admin")
        program = self.book.create_unit(unit_type="program", code="S", name="专题", parent_id=plan["unit_id"], actor="admin")
        project = self.book.create_unit(unit_type="project", code="J", name="项目", parent_id=program["unit_id"], leader="person:pm", actor="admin")
        topic_a = self.book.create_unit(unit_type="topic", code="A", name="课题甲", parent_id=project["unit_id"], leader="person:a", actor="admin")
        topic_b = self.book.create_unit(unit_type="topic", code="B", name="课题乙", parent_id=project["unit_id"], leader="person:b", actor="admin")
        lines = [{"topic_id": topic_a["unit_id"], "source_kind": "national", "source_ref": "N-1", "amount": budget_a}]
        if budget_b and budget_b != "0":
            lines.append({"topic_id": topic_b["unit_id"], "source_kind": "enterprise", "source_ref": "E-1", "amount": budget_b})
        self.book.freeze_budget(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=lines, actor="admin")
        return plan, program, project, topic_a, topic_b

    # ----- 层级与冻结 -----

    def test_hierarchy_and_guideline_freeze(self):
        plan, program, project, topic_a, topic_b = self._stack()
        self.assertEqual(plan["guideline_id"], "guide:g")
        self.assertEqual(plan["guideline_version"], 1)
        chain = [u["unit_type"] for u in self.book.ancestors(topic_a["unit_id"])]
        self.assertEqual(chain, ["project", "program", "plan"])
        self.assertEqual({c["code"] for c in self.book.children(project["unit_id"])}, {"A", "B"})
        # 新指南版本冻结后旧版本被取代，但历史版本仍可读
        self.book.freeze_guideline(guideline_id="guide:g", title="指南", content={"v": 2}, actor="admin")
        self.assertEqual(self.book.guideline("guide:g")["version"], 2)
        self.assertEqual(self.book.guideline("guide:g", version=1)["content_json"], '{"v":1}')

    def test_hierarchy_relations_enforced(self):
        with self.assertRaises(ValidationError):
            self.book.create_unit(unit_type="topic", code="X", name="无上级课题", actor="admin")
        plan = self.book.create_unit(unit_type="plan", code="P", name="计划", actor="admin")
        with self.assertRaises(ValidationError):  # 课题不能直接挂计划
            self.book.create_unit(unit_type="topic", code="X", name="越级", parent_id=plan["unit_id"], actor="admin")
        with self.assertRaises(NotFoundError):
            self.book.create_unit(unit_type="program", code="S", name="悬空", parent_id="plan:missing", actor="admin")

    def test_budget_versions_supersede_and_only_active_counts(self):
        _, _, project, topic_a, _ = self._stack()
        status = self.book.topic_budget_status(topic_a["unit_id"])
        self.assertEqual(status["budget_minor"], 100_000_000)
        # 再版：甲课题预算调整为 80 万，旧版本置为 superseded
        self.book.freeze_budget(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
            {"topic_id": topic_a["unit_id"], "source_kind": "national", "source_ref": "N-2", "amount": "800000.00"},
        ], actor="admin")
        self.assertEqual(self.book.topic_budget_status(topic_a["unit_id"])["budget_minor"], 80_000_000)
        versions = self.book.budget(f"budget:project:{project['unit_id']}")
        self.assertEqual(versions["version"], 2)

    # ----- 资金分录 -----

    def test_budget_lines_must_belong_to_unit(self):
        _, _, project, topic_a, _ = self._stack()
        other_plan = self.book.create_unit(unit_type="plan", code="P2", name="另一计划", actor="admin")
        other_program = self.book.create_unit(unit_type="program", code="S2", name="另一专题", parent_id=other_plan["unit_id"], actor="admin")
        other_project = self.book.create_unit(unit_type="project", code="J2", name="另一项目", parent_id=other_program["unit_id"], actor="admin")
        with self.assertRaises(ValidationError):  # 课题属于 project 而非 other_project
            self.book.freeze_budget(unit_type="project", unit_id=other_project["unit_id"], currency="CNY", lines=[
                {"topic_id": topic_a["unit_id"], "source_kind": "national", "source_ref": "X-1", "amount": "100.00"},
            ], actor="admin")
        with self.assertRaises(NotFoundError):  # 不存在的课题
            self.book.freeze_budget(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
                {"topic_id": "topic:missing", "source_kind": "national", "source_ref": "X-2", "amount": "100.00"},
            ], actor="admin")

    def test_appropriation_requires_frozen_source(self):
        _, _, _, topic_a, _ = self._stack()
        with self.assertRaises(ValidationError):  # 来源不在冻结预算
            self.book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="100.00", reference="r1", source_kind="provincial", source_ref="P-X", actor="t")
        self.book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="900000.00", reference="r1", source_kind="national", source_ref="N-1", actor="t")
        with self.assertRaises(ConflictError):  # 超出冻结额度
            self.book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="200000.00", reference="r2", source_kind="national", source_ref="N-1", actor="t")
        with self.assertRaises(ConflictError):  # 凭证参考号重复
            self.book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="1.00", reference="r1", source_kind="national", source_ref="N-1", actor="t")

    def test_expenditure_capacity_and_refund(self):
        _, _, _, topic_a, _ = self._stack()
        self.book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="阶段", leader="person:a", due_at="2027-01-01T00:00:00+08:00", hold_amount="950000.00")
        with self.assertRaises(ConflictError):  # 预留加支出超过预算
            self.book.post_fund(topic_id=topic_a["unit_id"], kind="expenditure", amount="60000.00", reference="e1", actor="t")
        self.book.post_fund(topic_id=topic_a["unit_id"], kind="expenditure", amount="50000.00", reference="e1", actor="t")
        with self.assertRaises(ConflictError):  # 退回不能超过净支出
            self.book.post_fund(topic_id=topic_a["unit_id"], kind="refund", amount="50001.00", reference="f1", actor="t")
        self.book.post_fund(topic_id=topic_a["unit_id"], kind="refund", amount="50000.00", reference="f1", actor="t")
        # 退回后额度恢复，同样支出可再次发生
        self.book.post_fund(topic_id=topic_a["unit_id"], kind="expenditure", amount="50000.00", reference="e2", actor="t")
        self.assertEqual(self.book.topic_budget_status(topic_a["unit_id"])["spent_minor"], 5_000_000)

    # ----- 里程碑与调整 -----

    def test_extension_only_affects_open_milestones(self):
        _, _, _, topic_a, _ = self._stack(budget_b="0")
        m1 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="一", leader="person:a", due_at="2027-01-01T00:00:00+08:00", hold_amount="10000.00")
        m2 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M2", title="二", leader="person:a", due_at="2027-03-01T00:00:00+08:00", hold_amount="10000.00")
        self.book.receive_acceptance(milestone_id=m1["milestone_id"], receipt_id="RCV-1", amount="10000.00")
        self.book.process_acceptance("RCV-1", actor="admin")
        adj = self.book.propose_adjustment(kind="extension", topic_id=topic_a["unit_id"], applicant="person:a", payload={"days": 30})
        with self.assertRaises(PermissionDenied):
            self.book.approve_adjustment(adj["adjustment_id"], reviewer="person:a", reason="自己同意")
        self.book.approve_adjustment(adj["adjustment_id"], reviewer="person:dean", reason="同意延期")
        result = self.book.apply_adjustment(adj["adjustment_id"])
        self.assertEqual(result["affected_open_milestones"], [m2["milestone_id"]])
        self.assertEqual(self.book.milestone(m1["milestone_id"])["due_at"], "2026-12-31T16:00:00Z")  # 已完成不变
        self.assertEqual(parse_instant(self.book.milestone(m2["milestone_id"])["due_at"]), parse_instant("2027-03-31T00:00:00+08:00"))
        # 已执行的调整不能重复执行
        with self.assertRaises(ConflictError):
            self.book.apply_adjustment(adj["adjustment_id"])

    def test_leader_change_and_merge_only_open_stages(self):
        _, _, _, topic_a, topic_b = self._stack()
        m1 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="一", leader="person:a", due_at="2027-01-01T00:00:00+08:00", hold_amount="10000.00")
        m2 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M2", title="二", leader="person:a", due_at="2027-02-01T00:00:00+08:00", hold_amount="10000.00")
        self.book.receive_acceptance(milestone_id=m1["milestone_id"], receipt_id="RCV-9", amount="10000.00")
        self.book.process_acceptance("RCV-9", actor="admin")
        change = self.book.propose_adjustment(kind="leader_change", topic_id=topic_a["unit_id"], applicant="person:a", payload={"new_leader": "person:a2"})
        # 新任负责人也不能审批自己相关的变更
        with self.assertRaises(PermissionDenied):
            self.book.approve_adjustment(change["adjustment_id"], reviewer="person:a2", reason="同意")
        self.book.approve_adjustment(change["adjustment_id"], reviewer="person:dean", reason="同意换人")
        self.book.apply_adjustment(change["adjustment_id"])
        self.assertEqual(self.book.milestone(m1["milestone_id"])["leader"], "person:a")   # 已完成不改
        self.assertEqual(self.book.milestone(m2["milestone_id"])["leader"], "person:a2")  # 未完成改
        merge = self.book.propose_adjustment(kind="merge", topic_id=topic_a["unit_id"], applicant="person:a2", payload={"into_topic_id": topic_b["unit_id"]})
        self.book.approve_adjustment(merge["adjustment_id"], reviewer="person:dean", reason="同意合并")
        applied = self.book.apply_adjustment(merge["adjustment_id"])
        self.assertEqual(applied["affected_open_milestones"], [m2["milestone_id"]])
        self.assertEqual(self.book.milestone(m2["milestone_id"])["topic_id"], topic_b["unit_id"])

    # ----- 专享占用 -----

    def test_restricted_asset_no_overlap(self):
        _, _, _, topic_a, topic_b = self._stack()
        self.book.occupy(asset_kind="sample", asset_id="sample:x", topic_id=topic_a["unit_id"], holder="h1", start_at="2027-01-01T09:00:00+08:00", end_at="2027-01-01T12:00:00+08:00")
        with self.assertRaises(ConflictError):  # 时间窗重叠
            self.book.occupy(asset_kind="sample", asset_id="sample:x", topic_id=topic_b["unit_id"], holder="h2", start_at="2027-01-01T11:00:00+08:00", end_at="2027-01-01T13:00:00+08:00")
        # 边界相接允许
        self.book.occupy(asset_kind="sample", asset_id="sample:x", topic_id=topic_b["unit_id"], holder="h2", start_at="2027-01-01T12:00:00+08:00", end_at="2027-01-01T13:00:00+08:00")
        occ = self.book.occupancies("sample:x")
        self.book.release_occupancy(occ[0]["occupancy_id"])
        # 释放后原窗口可重新占用
        self.book.occupy(asset_kind="sample", asset_id="sample:x", topic_id=topic_b["unit_id"], holder="h3", start_at="2027-01-01T09:30:00+08:00", end_at="2027-01-01T10:30:00+08:00")

    # ----- 设施使用与重复计费 -----

    def test_facility_use_charged_once(self):
        _, _, _, topic_a, _ = self._stack()
        with self.assertRaises(ConflictError):  # 同一计费参考号不得登记两次
            for ref in ("BILL-1", "BILL-1"):
                self.book.record_facility_use(facility_id="fac:sem", topic_id=topic_a["unit_id"], start_at="2027-01-01T09:00:00+08:00", end_at="2027-01-01T10:00:00+08:00", charge="100.00", charge_reference=ref, used_by="u")
        usage = self.book.record_facility_use(facility_id="fac:sem", topic_id=topic_a["unit_id"], start_at="2027-01-01T09:00:00+08:00", end_at="2027-01-01T10:00:00+08:00", charge="100.00", charge_reference="BILL-2", used_by="u")
        charged = self.book.charge_facility_use(usage["usage_id"], actor="admin")
        with self.assertRaises(ConflictError):  # 不得重复出账
            self.book.charge_facility_use(usage["usage_id"], actor="admin")
        rows = self.book.facility_usage(facility_id="fac:sem")
        billed = next(r for r in rows if r["charge_reference"] == "BILL-2")
        self.assertEqual(billed["charged_entry_id"], charged["entry_id"])

    # ----- 资料授权 -----

    def test_partner_sees_only_granted_material(self):
        _, _, _, topic_a, _ = self._stack()
        material = self.book.register_material(topic_id=topic_a["unit_id"], title="数据包", classification="restricted", fields=["a", "b"])
        # 未授权不可见
        self.assertFalse(self.book.view_material(material_id=material["material_id"], org_id="org:partner")["accessible"])
        with self.assertRaises(ValidationError):  # 授权字段超出资料范围
            self.book.grant_material(material_id=material["material_id"], org_id="org:partner", fields=["c"], valid_until="2027-12-31T23:59:59+08:00")
        self.book.grant_material(material_id=material["material_id"], org_id="org:partner", fields=["a"], valid_until="2026-10-01T23:59:59+08:00")
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-02T10:00:00+08:00")
        with self.assertRaises(PermissionDenied):  # 授权过期
            later.portfolio.view_material(material_id=material["material_id"], org_id="org:partner")

    # ----- 合同与贡献比例 -----

    def test_contract_shares_and_contributions(self):
        _, _, project, topic_a, topic_b = self._stack()
        with self.assertRaises(ValidationError):  # 分成合计必须 100%
            self.book.register_contract(title="合同", amount="100.00", topic_shares={topic_a["unit_id"]: "70.00", topic_b["unit_id"]: "20.00"})
        contract = self.book.register_contract(title="合同", amount="100.00", refs={"national": ["N-1"], "enterprise": ["E-1"]}, topic_shares={topic_a["unit_id"]: "70.00", topic_b["unit_id"]: "30.00"})
        with self.assertRaises(ValidationError):  # 贡献只能按项目登记
            self.book.save_contributions(result_type="contract", result_id=contract["contract_id"], shares={topic_a["unit_id"]: "100.00"})
        shares = self.book.save_contributions(result_type="contract", result_id=contract["contract_id"], shares={project["unit_id"]: "100.00"})
        self.assertEqual(shares["shares"][0]["share_bp"], 10_000)
        with self.assertRaises(ConflictError):  # 贡献比例冻结后不可改写
            self.book.save_contributions(result_type="contract", result_id=contract["contract_id"], shares={project["unit_id"]: "100.00"})

    def test_drill_down_from_contract(self):
        _, _, project, topic_a, topic_b = self._stack()
        self.book.post_fund(topic_id=topic_a["unit_id"], kind="appropriation", amount="500000.00", reference="rr", source_kind="national", source_ref="N-1", actor="t")
        usage = self.book.record_facility_use(facility_id="fac:x", topic_id=topic_a["unit_id"], start_at="2027-01-01T09:00:00+08:00", end_at="2027-01-01T10:00:00+08:00", charge="200.00", charge_reference="B1", used_by="person:a")
        self.book.charge_facility_use(usage["usage_id"], actor="admin")
        contract = self.book.register_contract(title="合同", amount="100.00", topic_shares={topic_a["unit_id"]: "100.00"})
        drill = self.book.drill_down(contract["contract_id"])
        topic = drill["topics"][0]
        self.assertEqual(topic["leader"], "person:a")
        self.assertEqual(len(topic["fund_entries"]), 2)  # 拨款 + 设施支出
        self.assertEqual(len(topic["facility_usage"]), 1)
        self.assertEqual([u["unit_type"] for u in topic["chain"]], ["project", "program", "plan"])

    # ----- 验收回执 -----

    def test_duplicate_receipt_enters_review_and_never_releases_twice(self):
        _, _, _, topic_a, _ = self._stack(budget_b="0")
        m1 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="一", leader="person:a", due_at="2027-01-01T00:00:00+08:00", hold_amount="10000.00")
        m2 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M2", title="二", leader="person:a", due_at="2027-02-01T00:00:00+08:00", hold_amount="10000.00")
        self.book.receive_acceptance(milestone_id=m1["milestone_id"], receipt_id="RCV-DUP", amount="10000.00")
        first = self.book.process_acceptance("RCV-DUP", actor="admin")
        self.assertFalse(first["replayed"])
        replay = self.book.process_acceptance("RCV-DUP", actor="admin")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entry_id"], first["entry_id"])
        # 同一回执再次提交：进入复核，资金未被二次释放
        with self.assertRaises(ConflictError):
            self.book.receive_acceptance(milestone_id=m2["milestone_id"], receipt_id="RCV-DUP", amount="10000.00")
        open_conflicts = self.book.conflicts(state="open")
        self.assertEqual(len(open_conflicts), 1)
        self.assertEqual(open_conflicts[0]["kind"], "duplicate_receipt")
        self.assertEqual(self.book.milestone(m2["milestone_id"])["state"], "open")
        releases = [e for e in self.book.fund_entries(topic_a["unit_id"]) if e["kind"] == "release"]
        self.assertEqual(len(releases), 1)
        resolved = self.book.resolve_conflict(open_conflicts[0]["conflict_id"], resolution="驳回重复回执", actor="auditor")
        self.assertEqual(resolved["state"], "resolved")
        with self.assertRaises(ConflictError):  # 已复核不能重复处理
            self.book.resolve_conflict(open_conflicts[0]["conflict_id"], resolution="再次处理", actor="auditor")

    # ----- 中断接续 -----

    def test_carryover_resumes_after_interruption(self):
        _, _, project, topic_a, topic_b = self._stack()
        self.app.workflows.request_carryover(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
            {"topic_id": topic_a["unit_id"], "source_kind": "carried", "source_ref": "carry-2028", "amount": "1000000.00"},
            {"topic_id": topic_a["unit_id"], "source_kind": "national", "source_ref": "N-2028", "amount": "300000.00"},
            {"topic_id": topic_b["unit_id"], "source_kind": "enterprise", "source_ref": "E-2028", "amount": "200000.00"},
        ], actor="admin", request_key="carry-2028")
        # 模拟 worker 领取任务后崩溃（不 finish）：租约 30 秒
        self.app.jobs.claim_due()
        # 租约过期后，新进程接续原任务
        later = CivicFlow.open(self.db_path, fixed_now="2026-10-02T09:01:00+08:00")
        results = later.workflows.run_due()
        self.assertEqual(results[0]["state"], "succeeded")
        self.assertEqual(results[0]["output"]["version"], 2)
        # 同请求键再次结转：幂等返回已冻结版本，不产生新版本
        again = later.workflows.carry_over_now(unit_type="project", unit_id=project["unit_id"], currency="CNY", lines=[
            {"topic_id": topic_a["unit_id"], "source_kind": "carried", "source_ref": "carry-2028", "amount": "1000000.00"},
        ], actor="admin", request_key="carry-2028")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["version"], 2)
        self.assertEqual(later.portfolio.topic_budget_status(topic_a["unit_id"])["budget_minor"], 130_000_000)  # 100万结转 + 30万新增

    def test_acceptance_job_is_idempotent(self):
        _, _, _, topic_a, _ = self._stack(budget_b="0")
        m1 = self.book.add_milestone(topic_id=topic_a["unit_id"], code="M1", title="一", leader="person:a", due_at="2027-01-01T00:00:00+08:00", hold_amount="10000.00")
        self.app.workflows.queue_acceptance(milestone_id=m1["milestone_id"], receipt_id="RCV-JOB", amount="10000.00")
        first = self.app.workflows.run_due()
        self.assertEqual(first[0]["state"], "succeeded")
        self.assertEqual(self.app.workflows.run_due(), [])
        # 手动重放处理同一回执：返回同一分录
        replay = self.book.process_acceptance("RCV-JOB", actor="x")
        self.assertEqual(replay["entry_id"], first[0]["output"]["entry_id"])

    # ----- 其他校验 -----

    def test_participant_roles_unique(self):
        _, _, _, topic_a, _ = self._stack()
        self.book.add_participant(org_id="org:u", unit_id=topic_a["unit_id"], role="lead")
        with self.assertRaises(ConflictError):
            self.book.add_participant(org_id="org:u", unit_id=topic_a["unit_id"], role="support")
        with self.assertRaises(ValidationError):
            self.book.add_participant(org_id="org:v", unit_id=topic_a["unit_id"], role="bogus")

    def test_approval_chain_recorded(self):
        _, _, project, _, _ = self._stack()
        self.book.record_approval(subject_type="unit", subject_id=project["unit_id"], reviewer="person:r1", decision="approved", reason="立项通过")
        self.book.record_approval(subject_type="unit", subject_id=project["unit_id"], reviewer="person:r2", decision="approved", reason="复核通过")
        chain = self.book.approval_chain("unit", project["unit_id"])
        self.assertEqual([a["step"] for a in chain], [1, 2])
        self.assertEqual([a["reviewer"] for a in chain], ["person:r1", "person:r2"])


if __name__ == "__main__":
    unittest.main()
