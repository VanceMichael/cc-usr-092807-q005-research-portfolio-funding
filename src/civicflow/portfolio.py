"""研发组合管理：计划、专题、项目、课题的全链条关系与资金/设施/样品管控。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Mapping

from .database import Database
from .errors import ConflictError, InvariantViolation, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .ledger import to_minor
from .timeutil import Clock, canonical_instant, parse_instant

UNIT_TYPES = ("plan", "program", "project", "topic")
UNIT_CHILDREN = {"plan": "program", "program": "project", "project": "topic", "topic": None}
FUND_KINDS = ("appropriation", "matching", "expenditure", "refund", "release")
POSITIVE_FUNDS = {"appropriation", "matching", "refund", "release"}
BUDGET_STATES = ("frozen", "active", "superseded")
GUIDELINE_STATES = ("frozen", "superseded")
ADJUSTMENT_KINDS = ("extension", "leader_change", "merge")
ADJUSTMENT_STATES = ("proposed", "approved", "rejected", "applied")
SHARE_BP_TOTAL = 10_000  # 贡献比例基点：100.00%


class _ContentConflict(Exception):
    """事务内信号：回滚业务改动，并在独立事务留存复核记录。"""

    def __init__(self, kind: str, subject_ref: str, detail: dict):
        super().__init__(kind)
        self.kind = kind
        self.subject_ref = subject_ref
        self.detail = detail


@dataclass(frozen=True)
class PortfolioBook:
    """研发组合登记册，所有写操作都在单个立即事务内完成。"""

    database: Database
    clock: Clock

    # ----- 适用指南：发布即冻结 -----

    def freeze_guideline(self, *, guideline_id: str, title: str, content: dict, actor: str) -> dict:
        require_safe(guideline_id, "指南标识")
        with self.database.transaction() as c:
            version = int(c.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM pf_guidelines WHERE guideline_id=?", (guideline_id,)).fetchone()["v"])
            c.execute("UPDATE pf_guidelines SET state='superseded' WHERE guideline_id=? AND state='frozen'", (guideline_id,))
            c.execute("INSERT INTO pf_guidelines(guideline_id,version,title,content_json,state,created_at,created_by) VALUES(?,?,?,?,?,?,?)", (guideline_id, version, title, canonical_json(content), "frozen", self.clock.now(), actor))
            return {"guideline_id": guideline_id, "version": version, "state": "frozen"}

    def guideline(self, guideline_id: str, *, version: int | None = None) -> dict:
        with self.database.connect() as c:
            row = self._guideline_row(c, guideline_id, version)
            result = dict(row)
            result["content"] = json.loads(row["content_json"])
            return result

    @staticmethod
    def _guideline_row(c, guideline_id: str, version: int | None):
        if version is not None:
            row = c.execute("SELECT * FROM pf_guidelines WHERE guideline_id=? AND version=?", (guideline_id, version)).fetchone()
        else:
            row = c.execute("SELECT * FROM pf_guidelines WHERE guideline_id=? AND state='frozen' ORDER BY version DESC LIMIT 1", (guideline_id,)).fetchone()
        if not row:
            raise NotFoundError("适用指南版本不存在")
        return row

    # ----- 预算版本：冻结后不可改写，只能再版结转 -----

    def freeze_budget(self, *, unit_type: str, unit_id: str, currency: str, lines: list[dict], carried_from: str | None = None, actor: str) -> dict:
        self._require_unit_type(unit_type)
        budget_id = f"budget:{unit_type}:{unit_id}"
        clean = self._clean_lines(lines)
        with self.database.transaction() as c:
            existing = c.execute("SELECT * FROM pf_units WHERE unit_type=? AND unit_id=?", (unit_type, unit_id)).fetchone()
            if not existing:
                raise NotFoundError(f"{unit_type}/{unit_id} 不存在，无法冻结预算")
            for line in clean:
                topic = c.execute("SELECT * FROM pf_units WHERE unit_type='topic' AND unit_id=?", (line["topic_id"],)).fetchone()
                if not topic:
                    raise NotFoundError(f"课题 {line['topic_id']} 不存在")
                if not self._is_descendant(c, line["topic_id"], unit_id):
                    raise ValidationError(f"课题 {line['topic_id']} 不属于 {unit_type}/{unit_id}，预算不能越权挂载")
            version = int(c.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM pf_budgets WHERE budget_id=?", (budget_id,)).fetchone()["v"])
            total = sum(line["amount_minor"] for line in clean)
            now = self.clock.now()
            c.execute("UPDATE pf_budgets SET state='superseded' WHERE budget_id=? AND state='active'", (budget_id,))
            c.execute("INSERT INTO pf_budgets(budget_id,version,unit_type,unit_id,currency,carried_from,state,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)", (budget_id, version, unit_type, unit_id, currency, carried_from, "active", now, actor))
            for line in clean:
                c.execute("INSERT INTO pf_budget_lines(budget_id,version,topic_id,source_kind,source_ref,amount_minor) VALUES(?,?,?,?,?,?)", (budget_id, version, line["topic_id"], line["source_kind"], line["source_ref"], line["amount_minor"]))
            c.execute("UPDATE pf_units SET budget_id=?,budget_version=?,updated_at=? WHERE unit_type=? AND unit_id=?", (budget_id, version, now, unit_type, unit_id))
            return {"budget_id": budget_id, "version": version, "total_minor": total, "currency": currency, "state": "active", "carried_from": carried_from}

    @staticmethod
    def _clean_lines(lines: list[dict]) -> list[dict]:
        if not lines:
            raise ValidationError("预算至少包含一条来源分录")
        clean: list[dict] = []
        seen: set[tuple] = set()
        for raw in lines:
            topic_id = require_safe(str(raw.get("topic_id", "")), "课题标识")
            source_kind = str(raw.get("source_kind", ""))
            if source_kind not in {"national", "provincial", "enterprise", "local", "self_raised", "carried"}:
                raise ValidationError("预算来源类型不合法")
            source_ref = require_safe(str(raw.get("source_ref", "")), "来源参考号")
            amount = to_minor(raw.get("amount", 0))
            if amount <= 0:
                raise ValidationError("预算金额必须大于零")
            key = (topic_id, source_kind, source_ref)
            if key in seen:
                raise ValidationError("同一预算版本内来源分录重复")
            seen.add(key)
            clean.append({"topic_id": topic_id, "source_kind": source_kind, "source_ref": source_ref, "amount_minor": amount})
        return clean

    def budget(self, budget_id: str, *, version: int | None = None) -> dict:
        with self.database.connect() as c:
            if version is None:
                row = c.execute("SELECT * FROM pf_budgets WHERE budget_id=? ORDER BY version DESC LIMIT 1", (budget_id,)).fetchone()
            else:
                row = c.execute("SELECT * FROM pf_budgets WHERE budget_id=? AND version=?", (budget_id, version)).fetchone()
            if not row:
                raise NotFoundError("预算版本不存在")
            lines = [dict(r) for r in c.execute("SELECT * FROM pf_budget_lines WHERE budget_id=? AND version=? ORDER BY topic_id,source_kind,source_ref", (budget_id, row["version"]))]
            result = dict(row)
            result["lines"] = lines
            result["total_minor"] = sum(int(r["amount_minor"]) for r in lines)
            return result

    def budget_by_carry_key(self, carried_from: str) -> dict | None:
        """按结转请求键找回已冻结版本，供中断接续时幂等重放。"""
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM pf_budgets WHERE carried_from=?", (carried_from,)).fetchone()
            if not row:
                return None
            return self.budget(row["budget_id"], version=row["version"])

    # ----- 层级单元：计划/专题/项目/课题 -----

    def create_unit(self, *, unit_type: str, code: str, name: str, parent_id: str | None = None, leader: str | None = None, guideline: tuple[str, int] | None = None, actor: str) -> dict:
        self._require_unit_type(unit_type)
        if parent_id is not None:
            parent_type = parent_id.split(":", 1)[0]
            if UNIT_CHILDREN.get(parent_type) != unit_type:
                raise ValidationError(f"{unit_type} 必须挂在 {UNIT_CHILDREN} 允许的上级下")
        elif unit_type != "plan":
            raise ValidationError("只有计划可以没有上级")
        unit_id = new_id(unit_type)
        now = self.clock.now()
        with self.database.transaction() as c:
            if parent_id is not None and not c.execute("SELECT 1 FROM pf_units WHERE unit_id=?", (parent_id,)).fetchone():
                raise NotFoundError(f"上级单元 {parent_id} 不存在")
            g_id = g_ver = None
            if guideline is not None:
                g_id, g_ver = guideline
                row = self._guideline_row(c, g_id, g_ver)
                g_id, g_ver = row["guideline_id"], row["version"]
            c.execute("INSERT INTO pf_units(unit_type,unit_id,code,name,parent_id,leader,state,guideline_id,guideline_version,budget_id,budget_version,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (unit_type, unit_id, code, name, parent_id, leader, "established", g_id, g_ver, None, None, now, now, actor))
            return self._unit_dict(c.execute("SELECT * FROM pf_units WHERE unit_id=?", (unit_id,)).fetchone())

    def unit(self, unit_id: str) -> dict:
        with self.database.connect() as c:
            return self._unit_dict(self._unit_row(c, unit_id))

    def children(self, parent_id: str) -> list[dict]:
        with self.database.connect() as c:
            return [self._unit_dict(r) for r in c.execute("SELECT * FROM pf_units WHERE parent_id=? ORDER BY code,unit_id", (parent_id,))]

    def ancestors(self, unit_id: str) -> list[dict]:
        chain: list[dict] = []
        with self.database.connect() as c:
            current = self._unit_row(c, unit_id)
            while current["parent_id"]:
                current = self._unit_row(c, current["parent_id"])
                chain.append(self._unit_dict(current))
        return chain

    # ----- 承担单位 -----

    def add_participant(self, *, org_id: str, unit_id: str, role: str, joined_at: str | None = None) -> dict:
        require_safe(org_id, "单位标识")
        if role not in {"lead", "collaborator", "support"}:
            raise ValidationError("承担角色不合法")
        participant_id = new_id("participant")
        with self.database.transaction() as c:
            unit = self._unit_row(c, unit_id)
            at = canonical_instant(joined_at) if joined_at else self.clock.now()
            try:
                c.execute("INSERT INTO pf_participants(participant_id,org_id,unit_type,unit_id,role,joined_at) VALUES(?,?,?,?,?,?)", (participant_id, org_id, unit["unit_type"], unit_id, role, at))
            except Exception as exc:  # 唯一约束
                raise ConflictError("该单位已经承担此单元") from exc
            return {"participant_id": participant_id, "org_id": org_id, "unit_id": unit_id, "role": role, "joined_at": at}

    def participants(self, unit_id: str) -> list[dict]:
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pf_participants WHERE unit_id=? ORDER BY role,org_id", (unit_id,))]

    # ----- 里程碑（阶段）与未完成阶段的预算占用 -----

    def add_milestone(self, *, topic_id: str, code: str, title: str, leader: str, due_at: str, hold_amount: str) -> dict:
        minor = to_minor(hold_amount)
        if minor < 0:
            raise ValidationError("里程碑预留预算不能为负")
        due = canonical_instant(due_at)
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            if c.execute("SELECT 1 FROM pf_milestones WHERE topic_id=? AND code=?", (topic_id, code)).fetchone():
                raise ConflictError("课题下里程碑编号重复")
            self._assert_topic_capacity(c, topic_id, minor)
            milestone_id = new_id("milestone")
            c.execute("INSERT INTO pf_milestones(milestone_id,topic_id,code,title,leader,due_at,hold_minor,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (milestone_id, topic_id, code, title, leader, due, minor, "open", self.clock.now()))
            return self._milestone_dict(c.execute("SELECT * FROM pf_milestones WHERE milestone_id=?", (milestone_id,)).fetchone())

    def milestone(self, milestone_id: str) -> dict:
        with self.database.connect() as c:
            return self._milestone_dict(self._milestone_row(c, milestone_id))

    def milestones(self, topic_id: str, *, include_completed: bool = True) -> list[dict]:
        sql = "SELECT * FROM pf_milestones WHERE topic_id=?"
        if not include_completed:
            sql += " AND state='open'"
        sql += " ORDER BY due_at,code"
        with self.database.connect() as c:
            return [self._milestone_dict(r) for r in c.execute(sql, (topic_id,))]

    def topic_budget_status(self, topic_id: str) -> dict:
        """课题预算、已预留（未完成里程碑）、已支出与可用余额。"""
        with self.database.connect() as c:
            self._require_topic(c, topic_id)
            budget = self._topic_budget_minor(c, topic_id)
            held = int(c.execute("SELECT COALESCE(SUM(hold_minor),0) AS v FROM pf_milestones WHERE topic_id=? AND state='open'", (topic_id,)).fetchone()["v"])
            spent = self._net_spent(c, topic_id)
            released = self._fund_sum(c, topic_id, kinds=("release",))
            refunds = self._fund_sum(c, topic_id, kinds=("refund",))
            available = budget - held - spent
            return {"topic_id": topic_id, "budget_minor": budget, "held_open_minor": held, "spent_minor": spent, "refund_minor": refunds, "released_minor": released, "available_minor": available}

    # ----- 不可改写资金分录：拨款、配套、支出、退回 -----

    def post_fund(self, *, topic_id: str, kind: str, amount: str, reference: str, source_kind: str | None = None, source_ref: str | None = None, currency: str = "CNY", milestone_id: str | None = None, contract_id: str | None = None, receipt_id: str | None = None, actor: str) -> dict:
        if kind not in FUND_KINDS:
            raise ValidationError("资金分录类型不合法")
        minor = to_minor(amount)
        if minor <= 0:
            raise ValidationError("金额必须大于零")
        require_safe(reference, "凭证参考号")
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            if milestone_id is not None:
                row = self._milestone_row(c, milestone_id)
                if row["topic_id"] != topic_id:
                    raise ValidationError("里程碑不属于该课题")
            if contract_id is not None:
                self._require_contract(c, contract_id)
            if kind in {"appropriation", "matching"}:
                # 拨款/配套必须能落到冻结预算版本中的来源分录
                if not source_kind or not source_ref:
                    raise ValidationError("拨款与配套必须指明确切预算来源")
                self._assert_budget_source(c, topic_id, source_kind, source_ref, minor)
            if kind == "expenditure":
                self._assert_topic_capacity(c, topic_id, minor)
            if kind == "refund":
                net = self._net_spent(c, topic_id)
                if minor > net:
                    raise ConflictError("退回金额不能超过净支出")
            entry_id = new_id("fund")
            now = self.clock.now()
            try:
                c.execute("INSERT INTO pf_fund_entries(entry_id,topic_id,kind,source_kind,source_ref,currency,amount_minor,reference,milestone_id,contract_id,receipt_id,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (entry_id, topic_id, kind, source_kind, source_ref, currency, minor, reference, milestone_id, contract_id, receipt_id, now, actor))
            except Exception as exc:
                raise ConflictError("相同凭证参考号的分录已经存在，不能重复入账") from exc
            return {"entry_id": entry_id, "topic_id": topic_id, "kind": kind, "amount_minor": minor, "currency": currency, "reference": reference, "occurred_at": now}

    def fund_entries(self, topic_id: str) -> list[dict]:
        with self.database.connect() as c:
            self._require_topic(c, topic_id)
            return [dict(r) for r in c.execute("SELECT * FROM pf_fund_entries WHERE topic_id=? ORDER BY occurred_at,entry_id", (topic_id,))]

    # ----- 受限样品与大型设备：时间窗专享，禁止重叠占用 -----

    def occupy(self, *, asset_kind: str, asset_id: str, topic_id: str, holder: str, start_at: str, end_at: str) -> dict:
        if asset_kind not in {"sample", "equipment"}:
            raise ValidationError("资产类型必须是 sample 或 equipment")
        start = canonical_instant(start_at); end = canonical_instant(end_at)
        if parse_instant(start) >= parse_instant(end):
            raise ValidationError("占用结束时间必须晚于开始时间")
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            clash = c.execute("SELECT occupancy_id FROM pf_occupancies WHERE asset_kind=? AND asset_id=? AND status='active' AND start_at<? AND end_at>?", (asset_kind, asset_id, end, start)).fetchone()
            if clash:
                raise ConflictError(f"资产已在 {clash['occupancy_id']} 期间被占用，不能重叠")
            occupancy_id = new_id("occupancy")
            c.execute("INSERT INTO pf_occupancies(occupancy_id,asset_kind,asset_id,topic_id,holder,start_at,end_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (occupancy_id, asset_kind, asset_id, topic_id, holder, start, end, "active", self.clock.now()))
            return {"occupancy_id": occupancy_id, "asset_kind": asset_kind, "asset_id": asset_id, "topic_id": topic_id, "start_at": start, "end_at": end, "status": "active"}

    def release_occupancy(self, occupancy_id: str) -> dict:
        with self.database.transaction() as c:
            changed = c.execute("UPDATE pf_occupancies SET status='released' WHERE occupancy_id=? AND status='active'", (occupancy_id,)).rowcount
            if changed != 1:
                if not c.execute("SELECT 1 FROM pf_occupancies WHERE occupancy_id=?", (occupancy_id,)).fetchone():
                    raise NotFoundError("占用记录不存在")
                raise ConflictError("占用记录已释放")
            return dict(c.execute("SELECT * FROM pf_occupancies WHERE occupancy_id=?", (occupancy_id,)).fetchone())

    def occupancies(self, asset_id: str) -> list[dict]:
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pf_occupancies WHERE asset_id=? ORDER BY start_at", (asset_id,))]

    # ----- 共享实验平台：每次使用都登记，且不能被重复计费 -----

    def record_facility_use(self, *, facility_id: str, topic_id: str, start_at: str, end_at: str, charge: str, charge_reference: str, used_by: str) -> dict:
        start = canonical_instant(start_at); end = canonical_instant(end_at)
        if parse_instant(start) >= parse_instant(end):
            raise ValidationError("使用结束时间必须晚于开始时间")
        minor = to_minor(charge)
        if minor < 0:
            raise ValidationError("费用不能为负")
        require_safe(charge_reference, "计费参考号")
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            if c.execute("SELECT 1 FROM pf_facility_usage WHERE facility_id=? AND charge_reference=?", (facility_id, charge_reference)).fetchone():
                raise ConflictError("该设施使用计费参考号已存在，拒绝重复计费")
            usage_id = new_id("usage")
            now = self.clock.now()
            c.execute("INSERT INTO pf_facility_usage(usage_id,facility_id,topic_id,start_at,end_at,charge_minor,charge_reference,charged_entry_id,used_by,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (usage_id, facility_id, topic_id, start, end, minor, charge_reference, None, used_by, now))
            return dict(c.execute("SELECT * FROM pf_facility_usage WHERE usage_id=?", (usage_id,)).fetchone())

    def charge_facility_use(self, usage_id: str, *, actor: str) -> dict:
        """把一次设施使用落成支出分录；已计费的使用不能再次出账。"""
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM pf_facility_usage WHERE usage_id=?", (usage_id,)).fetchone()
            if not row:
                raise NotFoundError("设施使用记录不存在")
            if row["charged_entry_id"]:
                raise ConflictError("该设施使用已经计费，不能重复出账")
            self._assert_topic_capacity(c, row["topic_id"], int(row["charge_minor"]))
            entry_id = new_id("fund")
            now = self.clock.now()
            reference = f"{row['facility_id']}:{row['charge_reference']}"
            try:
                c.execute("INSERT INTO pf_fund_entries(entry_id,topic_id,kind,source_kind,source_ref,currency,amount_minor,reference,milestone_id,contract_id,receipt_id,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (entry_id, row["topic_id"], "expenditure", "facility", row["facility_id"], "CNY", row["charge_minor"], reference, None, None, None, now, actor))
            except Exception as exc:
                raise ConflictError("设施费用已经入账，拒绝重复计费") from exc
            c.execute("UPDATE pf_facility_usage SET charged_entry_id=? WHERE usage_id=?", (entry_id, usage_id))
            return {"usage_id": usage_id, "entry_id": entry_id, "amount_minor": row["charge_minor"]}

    def facility_usage(self, *, facility_id: str | None = None, topic_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM pf_facility_usage WHERE 1=1"; params: list[object] = []
        if facility_id is not None:
            sql += " AND facility_id=?"; params.append(facility_id)
        if topic_id is not None:
            sql += " AND topic_id=?"; params.append(topic_id)
        sql += " ORDER BY occurred_at,usage_id"
        with self.database.connect() as c:
            return [dict(r) for r in c.execute(sql, params)]

    # ----- 成果资料授权：合作单位只能接触获授权资料 -----

    def register_material(self, *, topic_id: str, title: str, classification: str, fields: Iterable[str]) -> dict:
        fields = sorted({f for f in fields if isinstance(f, str) and f})
        if not fields:
            raise ValidationError("资料至少登记一个字段")
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            material_id = new_id("material")
            c.execute("INSERT INTO pf_materials(material_id,topic_id,title,classification,fields_json,created_at) VALUES(?,?,?,?,?,?)", (material_id, topic_id, title, classification, canonical_json(fields), self.clock.now()))
            result = dict(c.execute("SELECT * FROM pf_materials WHERE material_id=?", (material_id,)).fetchone())
            result["fields"] = fields
            return result

    def grant_material(self, *, material_id: str, org_id: str, fields: Iterable[str], valid_until: str) -> dict:
        require_safe(org_id, "单位标识")
        valid_until = canonical_instant(valid_until)
        fields = sorted({f for f in fields if isinstance(f, str) and f})
        if not fields:
            raise ValidationError("授权至少包含一个字段")
        with self.database.transaction() as c:
            material = c.execute("SELECT * FROM pf_materials WHERE material_id=?", (material_id,)).fetchone()
            if not material:
                raise NotFoundError("资料不存在")
            known = set(json.loads(material["fields_json"]))
            unknown = set(fields) - known
            if unknown:
                raise ValidationError("授权字段超出资料登记范围: " + ", ".join(sorted(unknown)))
            grant_id = new_id("grant")
            now = self.clock.now()
            try:
                c.execute("INSERT INTO pf_material_grants(grant_id,material_id,org_id,fields_json,valid_until,state,granted_at) VALUES(?,?,?,?,?,?,?)", (grant_id, material_id, org_id, canonical_json(fields), valid_until, "granted", now))
            except Exception as exc:
                raise ConflictError("该单位已持有此资料的授权") from exc
            return {"grant_id": grant_id, "material_id": material_id, "org_id": org_id, "fields": fields, "valid_until": valid_until, "state": "granted"}

    def view_material(self, *, material_id: str, org_id: str) -> dict:
        with self.database.connect() as c:
            material = c.execute("SELECT * FROM pf_materials WHERE material_id=?", (material_id,)).fetchone()
            if not material:
                raise NotFoundError("资料不存在")
            grant = c.execute("SELECT * FROM pf_material_grants WHERE material_id=? AND org_id=? AND state='granted'", (material_id, org_id)).fetchone()
            result = {"material_id": material_id, "topic_id": material["topic_id"], "title": material["title"], "classification": material["classification"], "org_id": org_id}
            if not grant:
                result["accessible"] = False
                result["fields"] = []
                return result
            if parse_instant(grant["valid_until"]) < parse_instant(self.clock.now()):
                raise PermissionDenied("授权已经过期")
            result["accessible"] = True
            result["fields"] = json.loads(grant["fields_json"])
            result["valid_until"] = grant["valid_until"]
            return result

    # ----- 知识产权、技术合同与项目贡献比例 -----

    def register_ip(self, *, title: str, kind: str, topic_id: str) -> dict:
        if kind not in {"patent", "software", "paper", "standard", "knowhow"}:
            raise ValidationError("知识产权类型不合法")
        with self.database.transaction() as c:
            self._require_topic(c, topic_id)
            ip_id = new_id("ip")
            c.execute("INSERT INTO pf_ips(ip_id,title,kind,topic_id,state,created_at) VALUES(?,?,?,?,?,?)", (ip_id, title, kind, topic_id, "registered", self.clock.now()))
            return dict(c.execute("SELECT * FROM pf_ips WHERE ip_id=?", (ip_id,)).fetchone())

    def register_contract(self, *, title: str, amount: str, currency: str = "CNY", refs: Mapping[str, Iterable[str]] | None = None, topic_shares: Mapping[str, str] | None = None) -> dict:
        """登记技术合同；refs 引用国家任务/省专项/企业成果，topic_shares 给出课题收入分成，合计必须 100%。"""
        minor = to_minor(amount)
        if minor <= 0:
            raise ValidationError("合同金额必须大于零")
        shares = self._clean_shares(topic_shares)
        with self.database.transaction() as c:
            for topic_id in shares:
                self._require_topic(c, topic_id)
            for ref_id in (refs or {}).get("national", []) + (refs or {}).get("provincial", []) + (refs or {}).get("enterprise", []):
                require_safe(ref_id, "引用标识")
            contract_id = new_id("contract")
            now = self.clock.now()
            c.execute("INSERT INTO pf_contracts(contract_id,title,currency,amount_minor,state,created_at) VALUES(?,?,?,?,?,?)", (contract_id, title, currency, minor, "registered", now))
            for ref_kind, ids in (refs or {}).items():
                if ref_kind not in {"national", "provincial", "enterprise"}:
                    raise ValidationError("合同引用类型不合法")
                for ref_id in ids:
                    c.execute("INSERT INTO pf_contract_refs(contract_id,ref_kind,ref_id) VALUES(?,?,?)", (contract_id, ref_kind, ref_id))
            for topic_id, bp in shares.items():
                c.execute("INSERT INTO pf_contract_topics(contract_id,topic_id,share_bp) VALUES(?,?,?)", (contract_id, topic_id, bp))
            return self._contract_dict(c, contract_id)

    def contract(self, contract_id: str) -> dict:
        with self.database.connect() as c:
            self._require_contract(c, contract_id)
            return self._contract_dict(c, contract_id)

    def save_contributions(self, *, result_type: str, result_id: str, shares: Mapping[str, str]) -> dict:
        """成果转化时保存各项目贡献比例；只允许保存一次、合计必须 100%。"""
        if result_type not in {"ip", "contract"}:
            raise ValidationError("成果类型必须是 ip 或 contract")
        clean = self._clean_shares(shares)
        with self.database.transaction() as c:
            if result_type == "ip":
                if not c.execute("SELECT 1 FROM pf_ips WHERE ip_id=?", (result_id,)).fetchone():
                    raise NotFoundError("知识产权不存在")
            else:
                self._require_contract(c, result_id)
            for project_id in clean:
                row = c.execute("SELECT unit_type FROM pf_units WHERE unit_id=?", (project_id,)).fetchone()
                if not row or row["unit_type"] != "project":
                    raise ValidationError(f"{project_id} 不是项目")
            if c.execute("SELECT 1 FROM pf_contributions WHERE result_type=? AND result_id=?", (result_type, result_id)).fetchone():
                raise ConflictError("成果贡献比例已经冻结保存，不能改写")
            now = self.clock.now()
            for project_id, bp in clean.items():
                c.execute("INSERT INTO pf_contributions(result_type,result_id,project_id,share_bp,recorded_at) VALUES(?,?,?,?,?)", (result_type, result_id, project_id, bp, now))
            return {"result_type": result_type, "result_id": result_id, "shares": [{"project_id": p, "share_bp": b} for p, b in clean.items()]}

    def contributions(self, result_type: str, result_id: str) -> list[dict]:
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pf_contributions WHERE result_type=? AND result_id=? ORDER BY project_id", (result_type, result_id))]

    # ----- 重大调整：回避、批准链、仅影响未完成阶段 -----

    def propose_adjustment(self, *, kind: str, topic_id: str, applicant: str, payload: dict) -> dict:
        if kind not in ADJUSTMENT_KINDS:
            raise ValidationError("调整类型不合法")
        with self.database.transaction() as c:
            topic = self._require_topic(c, topic_id)
            if kind == "leader_change" and payload.get("new_leader") == topic["leader"]:
                raise ValidationError("新负责人不能与现任负责人相同")
            if kind == "merge" and not payload.get("into_topic_id"):
                raise ValidationError("任务合并必须给出目标课题")
            adjustment_id = new_id("adjustment")
            c.execute("INSERT INTO pf_adjustments(adjustment_id,kind,topic_id,payload_json,state,applicant,created_at,decided_at) VALUES(?,?,?,?,?,?,?,?)", (adjustment_id, kind, topic_id, canonical_json(payload), "proposed", applicant, self.clock.now(), None))
            return {"adjustment_id": adjustment_id, "kind": kind, "topic_id": topic_id, "state": "proposed", "applicant": applicant}

    def approve_adjustment(self, adjustment_id: str, *, reviewer: str, reason: str) -> dict:
        return self._decide_adjustment(adjustment_id, reviewer=reviewer, reason=reason, decision="approved")

    def reject_adjustment(self, adjustment_id: str, *, reviewer: str, reason: str) -> dict:
        return self._decide_adjustment(adjustment_id, reviewer=reviewer, reason=reason, decision="rejected")

    def _decide_adjustment(self, adjustment_id: str, *, reviewer: str, reason: str, decision: str) -> dict:
        if not reason.strip():
            raise ValidationError("审批必须填写意见")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM pf_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone()
            if not row:
                raise NotFoundError("调整不存在")
            if row["state"] != "proposed":
                raise ConflictError(f"调整当前状态为 {row['state']}，不能审批")
            if row["applicant"] == reviewer:
                raise PermissionDenied("项目负责人不得审批自己的重大调整")
            payload = json.loads(row["payload_json"])
            if row["kind"] == "leader_change" and payload.get("new_leader") == reviewer:
                raise PermissionDenied("项目负责人不得审批自己的重大调整")
            step = int(c.execute("SELECT COALESCE(MAX(step),0)+1 AS s FROM pf_approvals WHERE subject_type='adjustment' AND subject_id=?", (adjustment_id,)).fetchone()["s"])
            now = self.clock.now()
            c.execute("INSERT INTO pf_approvals(approval_id,subject_type,subject_id,step,reviewer,decision,reason,decided_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("approval"), "adjustment", adjustment_id, step, reviewer, decision, reason.strip(), now))
            c.execute("UPDATE pf_adjustments SET state=?,decided_at=? WHERE adjustment_id=?", (decision, now, adjustment_id))
            return {"adjustment_id": adjustment_id, "state": decision, "step": step}

    def record_approval(self, *, subject_type: str, subject_id: str, reviewer: str, decision: str, reason: str) -> dict:
        """为立项、成果等登记批准链节点（按步骤追加，不可改写）。"""
        if subject_type not in {"unit", "adjustment", "ip", "contract", "acceptance"}:
            raise ValidationError("批准对象类型不合法")
        if decision not in {"approved", "rejected"}:
            raise ValidationError("审批结论必须是 approved 或 rejected")
        if not reason.strip():
            raise ValidationError("审批必须填写意见")
        with self.database.transaction() as c:
            if subject_type == "unit" and not c.execute("SELECT 1 FROM pf_units WHERE unit_id=?", (subject_id,)).fetchone():
                raise NotFoundError("单元不存在")
            if subject_type == "ip" and not c.execute("SELECT 1 FROM pf_ips WHERE ip_id=?", (subject_id,)).fetchone():
                raise NotFoundError("知识产权不存在")
            if subject_type == "contract":
                self._require_contract(c, subject_id)
            step = int(c.execute("SELECT COALESCE(MAX(step),0)+1 AS s FROM pf_approvals WHERE subject_type=? AND subject_id=?", (subject_type, subject_id)).fetchone()["s"])
            approval_id = new_id("approval"); now = self.clock.now()
            c.execute("INSERT INTO pf_approvals(approval_id,subject_type,subject_id,step,reviewer,decision,reason,decided_at) VALUES(?,?,?,?,?,?,?,?)", (approval_id, subject_type, subject_id, step, reviewer, decision, reason.strip(), now))
            return {"approval_id": approval_id, "subject_type": subject_type, "subject_id": subject_id, "step": step, "reviewer": reviewer, "decision": decision}

    def apply_adjustment(self, adjustment_id: str) -> dict:
        """执行已批准调整：延期与负责人变更只作用于未完成里程碑；合并只迁移未完成阶段。"""
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM pf_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone()
            if not row:
                raise NotFoundError("调整不存在")
            if row["state"] != "approved":
                raise ConflictError("只有已批准的调整可以执行")
            payload = json.loads(row["payload_json"])
            affected: list[str] = []
            if row["kind"] == "extension":
                days = int(payload.get("days", 0))
                if days <= 0:
                    raise ValidationError("延期天数必须大于零")
                milestones = c.execute("SELECT * FROM pf_milestones WHERE topic_id=? AND state='open'", (row["topic_id"],)).fetchall()
                for m in milestones:
                    due = (parse_instant(m["due_at"]) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
                    c.execute("UPDATE pf_milestones SET due_at=? WHERE milestone_id=?", (due, m["milestone_id"]))
                    affected.append(m["milestone_id"])
            elif row["kind"] == "leader_change":
                new_leader = payload["new_leader"]
                c.execute("UPDATE pf_units SET leader=?,updated_at=? WHERE unit_id=?", (new_leader, self.clock.now(), row["topic_id"]))
                for m in c.execute("SELECT milestone_id FROM pf_milestones WHERE topic_id=? AND state='open'", (row["topic_id"],)).fetchall():
                    c.execute("UPDATE pf_milestones SET leader=? WHERE milestone_id=?", (new_leader, m["milestone_id"]))
                    affected.append(m["milestone_id"])
            elif row["kind"] == "merge":
                target = payload["into_topic_id"]
                self._require_topic(c, target)
                for m in c.execute("SELECT milestone_id FROM pf_milestones WHERE topic_id=? AND state='open'", (row["topic_id"],)).fetchall():
                    c.execute("UPDATE pf_milestones SET topic_id=? WHERE milestone_id=?", (target, m["milestone_id"]))
                    affected.append(m["milestone_id"])
            c.execute("UPDATE pf_adjustments SET state='applied' WHERE adjustment_id=?", (adjustment_id,))
            return {"adjustment_id": adjustment_id, "state": "applied", "affected_open_milestones": affected}

    def approval_chain(self, subject_type: str, subject_id: str) -> list[dict]:
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pf_approvals WHERE subject_type=? AND subject_id=? ORDER BY step", (subject_type, subject_id))]

    # ----- 验收回执：重复回执不得再次释放资金 -----

    def receive_acceptance(self, *, milestone_id: str, receipt_id: str, amount: str) -> dict:
        minor = to_minor(amount)
        if minor <= 0:
            raise ValidationError("验收金额必须大于零")
        require_safe(receipt_id, "回执号")
        try:
            with self.database.transaction() as c:
                milestone = self._milestone_row(c, milestone_id)
                duplicate = c.execute("SELECT * FROM pf_acceptances WHERE receipt_id=?", (receipt_id,)).fetchone()
                if duplicate:
                    self._raise_conflict_note(c, kind="duplicate_receipt", subject_ref=receipt_id, detail={"existing_milestone": duplicate["milestone_id"], "incoming_milestone": milestone_id, "receipt_id": receipt_id})
                if milestone["state"] != "open":
                    raise ConflictError("里程碑已经验收，不能重复验收")
                c.execute("INSERT INTO pf_acceptances(receipt_id,milestone_id,amount_minor,state,received_at,processed_at,release_entry_id) VALUES(?,?,?,?,?,NULL,NULL)", (receipt_id, milestone_id, minor, "received", self.clock.now()))
                return {"receipt_id": receipt_id, "milestone_id": milestone_id, "amount_minor": minor, "state": "received"}
        except _ContentConflict as signal:
            conflict_id = self._persist_conflict(signal)
            raise ConflictError(f"内容冲突，进入复核（{conflict_id}）：{signal.kind}")

    def process_acceptance(self, receipt_id: str, *, actor: str) -> dict:
        """处理验收：关闭里程碑、释放预留、登记一笔释放分录；同一回执只会成功一次。"""
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM pf_acceptances WHERE receipt_id=?", (receipt_id,)).fetchone()
            if not row:
                raise NotFoundError("验收回执不存在")
            if row["state"] == "processed":
                return {"receipt_id": receipt_id, "entry_id": row["release_entry_id"], "replayed": True}
            milestone = self._milestone_row(c, row["milestone_id"])
            if milestone["state"] != "open":
                raise ConflictError("里程碑已经验收，不能重复释放资金")
            entry_id = new_id("fund")
            now = self.clock.now()
            reference = f"acceptance:{receipt_id}"
            c.execute("INSERT INTO pf_fund_entries(entry_id,topic_id,kind,source_kind,source_ref,currency,amount_minor,reference,milestone_id,contract_id,receipt_id,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (entry_id, milestone["topic_id"], "release", "acceptance", receipt_id, "CNY", row["amount_minor"], reference, milestone["milestone_id"], None, receipt_id, now, actor))
            c.execute("UPDATE pf_milestones SET state='completed' WHERE milestone_id=?", (milestone["milestone_id"],))
            c.execute("UPDATE pf_acceptances SET state='processed',processed_at=?,release_entry_id=? WHERE receipt_id=?", (now, entry_id, receipt_id))
            return {"receipt_id": receipt_id, "milestone_id": milestone["milestone_id"], "entry_id": entry_id, "amount_minor": row["amount_minor"], "replayed": False}

    # ----- 内容冲突复核 -----

    def conflicts(self, *, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM pf_conflicts WHERE 1=1"; params: list[object] = []
        if state:
            sql += " AND state=?"; params.append(state)
        sql += " ORDER BY created_at,conflict_id"
        with self.database.connect() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def resolve_conflict(self, conflict_id: str, *, resolution: str, actor: str) -> dict:
        if not resolution.strip():
            raise ValidationError("复核结论不能为空")
        with self.database.transaction() as c:
            changed = c.execute("UPDATE pf_conflicts SET state='resolved',resolved_at=? WHERE conflict_id=? AND state='open'", (self.clock.now(), conflict_id)).rowcount
            if changed != 1:
                if not c.execute("SELECT 1 FROM pf_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone():
                    raise NotFoundError("冲突记录不存在")
                raise ConflictError("冲突已经复核")
            return {"conflict_id": conflict_id, "state": "resolved", "resolution": resolution.strip(), "resolved_by": actor}

    def _raise_conflict_note(self, c, *, kind: str, subject_ref: str, detail: dict) -> None:
        raise _ContentConflict(kind, subject_ref, detail)

    def _persist_conflict(self, signal: _ContentConflict) -> str:
        with self.database.transaction() as c:
            conflict_id = new_id("conflict")
            c.execute("INSERT INTO pf_conflicts(conflict_id,kind,subject_ref,detail_json,state,created_at,resolved_at) VALUES(?,?,?,?,?,?,NULL)", (conflict_id, signal.kind, signal.subject_ref, canonical_json(signal.detail), "open", self.clock.now()))
        return conflict_id

    # ----- 合同下钻 -----

    def drill_down(self, contract_id: str) -> dict:
        """从合同金额下钻到课题收入分录、每笔资金、每次设施使用和责任人。"""
        with self.database.connect() as c:
            self._require_contract(c, contract_id)
            contract = dict(c.execute("SELECT * FROM pf_contracts WHERE contract_id=?", (contract_id,)).fetchone())
            shares = [dict(r) for r in c.execute("SELECT * FROM pf_contract_topics WHERE contract_id=? ORDER BY topic_id", (contract_id,))]
            refs = [dict(r) for r in c.execute("SELECT * FROM pf_contract_refs WHERE contract_id=? ORDER BY ref_kind,ref_id", (contract_id,))]
            topics = []
            for share in shares:
                topic_id = share["topic_id"]
                topic = self._unit_row(c, topic_id)
                funds = [dict(r) for r in c.execute("SELECT * FROM pf_fund_entries WHERE topic_id=? ORDER BY occurred_at,entry_id", (topic_id,))]
                facilities = [dict(r) for r in c.execute("SELECT * FROM pf_facility_usage WHERE topic_id=? ORDER BY occurred_at,usage_id", (topic_id,))]
                milestones = [self._milestone_dict(r) for r in c.execute("SELECT * FROM pf_milestones WHERE topic_id=? ORDER BY due_at,code", (topic_id,))]
                parents = []
                current = topic
                while current["parent_id"]:
                    current = self._unit_row(c, current["parent_id"])
                    parents.append({"unit_type": current["unit_type"], "unit_id": current["unit_id"], "name": current["name"], "leader": current["leader"]})
                topics.append({"topic_id": topic_id, "name": topic["name"], "leader": topic["leader"], "share_bp": share["share_bp"], "chain": parents, "fund_entries": funds, "facility_usage": facilities, "milestones": milestones})
            contract["refs"] = refs
            contract["topics"] = topics
            return contract

    # ----- 内部校验 -----

    @staticmethod
    def _require_unit_type(unit_type: str) -> None:
        if unit_type not in UNIT_TYPES:
            raise ValidationError(f"单元类型必须是 {UNIT_TYPES} 之一")

    @staticmethod
    def _unit_row(c, unit_id: str):
        row = c.execute("SELECT * FROM pf_units WHERE unit_id=?", (unit_id,)).fetchone()
        if not row:
            raise NotFoundError(f"单元 {unit_id} 不存在")
        return row

    def _require_topic(self, c, topic_id: str):
        row = self._unit_row(c, topic_id)
        if row["unit_type"] != "topic":
            raise ValidationError(f"{topic_id} 不是课题")
        return row

    @staticmethod
    def _is_descendant(c, unit_id: str, ancestor_id: str) -> bool:
        current = c.execute("SELECT parent_id FROM pf_units WHERE unit_id=?", (unit_id,)).fetchone()
        while current and current["parent_id"]:
            if current["parent_id"] == ancestor_id:
                return True
            current = c.execute("SELECT parent_id FROM pf_units WHERE unit_id=?", (current["parent_id"],)).fetchone()
        return False

    @staticmethod
    def _milestone_row(c, milestone_id: str):
        row = c.execute("SELECT * FROM pf_milestones WHERE milestone_id=?", (milestone_id,)).fetchone()
        if not row:
            raise NotFoundError("里程碑不存在")
        return row

    @staticmethod
    def _require_contract(c, contract_id: str):
        row = c.execute("SELECT * FROM pf_contracts WHERE contract_id=?", (contract_id,)).fetchone()
        if not row:
            raise NotFoundError("技术合同不存在")
        return row

    def _topic_budget_minor(self, c, topic_id: str) -> int:
        """汇总所有冻结预算版本中该课题的来源金额（重复再版时只取现行 active 版本）。"""
        row = c.execute(
            "SELECT COALESCE(SUM(l.amount_minor),0) AS v FROM pf_budget_lines l JOIN pf_budgets b ON l.budget_id=b.budget_id AND l.version=b.version WHERE l.topic_id=? AND b.state='active'",
            (topic_id,),
        ).fetchone()
        return int(row["v"])

    def _assert_topic_capacity(self, c, topic_id: str, extra_minor: int) -> None:
        budget = self._topic_budget_minor(c, topic_id)
        held = int(c.execute("SELECT COALESCE(SUM(hold_minor),0) AS v FROM pf_milestones WHERE topic_id=? AND state='open'", (topic_id,)).fetchone()["v"])
        spent = self._net_spent(c, topic_id)
        if held + spent + extra_minor > budget:
            raise ConflictError(f"课题 {topic_id} 预算不足：预算 {budget}，已预留 {held}，已支出 {spent}，本次 {extra_minor}")

    def _net_spent(self, c, topic_id: str) -> int:
        """支出减去支出退回后的净额，永不小于零。"""
        expenditure = self._fund_sum(c, topic_id, kinds=("expenditure",))
        refunds = self._fund_sum(c, topic_id, kinds=("refund",))
        return max(0, expenditure - refunds)

    def _assert_budget_source(self, c, topic_id: str, source_kind: str, source_ref: str, amount_minor: int) -> None:
        budgeted = int(c.execute(
            "SELECT COALESCE(SUM(l.amount_minor),0) AS v FROM pf_budget_lines l JOIN pf_budgets b ON l.budget_id=b.budget_id AND l.version=b.version WHERE l.topic_id=? AND l.source_kind=? AND l.source_ref=? AND b.state='active'",
            (topic_id, source_kind, source_ref),
        ).fetchone()["v"])
        if budgeted <= 0:
            raise ValidationError("拨款来源不在任何冻结预算版本中")
        used = int(c.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS v FROM pf_fund_entries WHERE topic_id=? AND kind IN ('appropriation','matching') AND source_kind=? AND source_ref=?",
            (topic_id, source_kind, source_ref),
        ).fetchone()["v"])
        if used + amount_minor > budgeted:
            raise ConflictError("拨款金额超过该预算来源的冻结额度")

    @staticmethod
    def _fund_sum(c, topic_id: str, *, kinds: Iterable[str]) -> int:
        kinds = tuple(kinds)
        placeholders = ",".join("?" for _ in kinds)
        row = c.execute(f"SELECT COALESCE(SUM(amount_minor),0) AS v FROM pf_fund_entries WHERE topic_id=? AND kind IN ({placeholders})", (topic_id, *kinds)).fetchone()
        return int(row["v"])

    @staticmethod
    def _clean_shares(topic_shares: Mapping[str, str] | None) -> dict[str, int]:
        if not topic_shares:
            raise ValidationError("必须给出分成比例")
        clean: dict[str, int] = {}
        for key, value in topic_shares.items():
            bp = to_minor(value, exponent=2)  # 百分比 ×100，即基点；允许到 0.01%
            if bp <= 0:
                raise ValidationError("分成比例必须大于零")
            clean[key] = bp
        if sum(clean.values()) != SHARE_BP_TOTAL:
            raise ValidationError("分成比例合计必须为 100%")
        return clean

    @staticmethod
    def _unit_dict(row) -> dict:
        return {"unit_type": row["unit_type"], "unit_id": row["unit_id"], "code": row["code"], "name": row["name"], "parent_id": row["parent_id"], "leader": row["leader"], "state": row["state"], "guideline_id": row["guideline_id"], "guideline_version": row["guideline_version"], "budget_id": row["budget_id"], "budget_version": row["budget_version"], "created_at": row["created_at"], "updated_at": row["updated_at"], "created_by": row["created_by"]}

    @staticmethod
    def _milestone_dict(row) -> dict:
        return {"milestone_id": row["milestone_id"], "topic_id": row["topic_id"], "code": row["code"], "title": row["title"], "leader": row["leader"], "due_at": row["due_at"], "hold_minor": row["hold_minor"], "state": row["state"], "created_at": row["created_at"]}

    def _contract_dict(self, c, contract_id: str) -> dict:
        result = dict(c.execute("SELECT * FROM pf_contracts WHERE contract_id=?", (contract_id,)).fetchone())
        result["refs"] = [dict(r) for r in c.execute("SELECT * FROM pf_contract_refs WHERE contract_id=? ORDER BY ref_kind,ref_id", (contract_id,))]
        result["topic_shares"] = [dict(r) for r in c.execute("SELECT * FROM pf_contract_topics WHERE contract_id=? ORDER BY topic_id", (contract_id,))]
        return result
