"""研发组合管理:计划、专题、项目、课题、里程碑与成果关系。

在通用实体仓储之上维护研发组合的结构关系与关键不变量:

- 计划 → 专题 → 项目 → 课题 → 里程碑逐级引用,创建时校验上级存在;
- 立项或重大调整时冻结适用指南与预算版本,历史版本由实体版本表保留;
- 延期、负责人变更、任务合并只作用于未完成阶段(待办里程碑);
- 项目负责人不得审批自己项目的重大调整;
- 合作单位只能接触获授权资料(按 org: 范围裁剪);
- 受限样品与大型设备容量为一,不能重叠占用;
- 成果转化记录各项目贡献比例,比例之和必须为一。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .errors import ConflictError, PermissionDenied, ValidationError
from .ledger import to_minor
from .repository import EntityRepository
from .reservations import ReservationBook
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant


ORGANIZATIONS = "organizations"
PROGRAMS = "programs"
TOPICS = "topics"
PROJECTS = "projects"
TASKS = "tasks"
MILESTONES = "milestones"
BUDGET_SOURCES = "budget_sources"
EQUIPMENT = "equipment"
IP_RIGHTS = "ip_rights"
CONTRACTS = "contracts"
MATERIALS = "materials"
TRANSFERS = "transfers"
ADJUSTMENTS = "adjustments"
REVIEWS = "reviews"

BUDGET_CHANNELS = ("国家任务", "省级专项", "企业合作", "其他")
EQUIPMENT_KINDS = ("大型设备", "受限样品", "普通设备")
RESTRICTED_EQUIPMENT_KINDS = ("大型设备", "受限样品")
ADJUSTMENT_KINDS = ("delay", "pi_change", "merge", "budget", "guideline")


def _text(values: Mapping[str, object], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 不能为空")
    return value.strip()


def _money(values: Mapping[str, object], field: str, *, allow_zero: bool = False) -> int:
    minor = to_minor(str(values.get(field, "")))
    if minor < 0 or (minor == 0 and not allow_zero):
        raise ValidationError(f"{field} 必须大于零" if not allow_zero else f"{field} 不能为负")
    return minor


def _ratio(value: object) -> Decimal:
    try:
        ratio = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("贡献比例格式错误") from exc
    if not ratio.is_finite() or ratio <= 0 or ratio > 1:
        raise ValidationError("贡献比例必须在 (0, 1] 之间")
    return ratio


def _year(values: Mapping[str, object], field: str = "fiscal_year") -> int:
    value = values.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 2000 or value > 2200:
        raise ValidationError(f"{field} 必须是有效年度")
    return value


@dataclass(frozen=True)
class PortfolioService:
    """研发组合结构、冻结与重大调整服务。"""

    repository: EntityRepository
    reservations: ReservationBook
    clock: Clock

    # ---- 计划与专题 ----

    def create_program(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        payload = {
            "code": _text(values, "code"),
            "title": _text(values, "title"),
            "guideline_ref": _text(values, "guideline_ref"),
            "guideline_version": _text(values, "guideline_version"),
            "owner_org": _text(values, "owner_org"),
            "state": "draft",
        }
        return self.repository.create(PROGRAMS, payload, actor=context.actor_id, request_key=request_key)

    def publish_program(self, context: AccessContext, program_id: str, *, expected_version: int, request_key: str) -> dict:
        context.require("write:portfolio")
        program = self.repository.get(PROGRAMS, program_id)
        if program["state"] != "draft":
            raise ConflictError("只有草稿计划可以发布")
        return self.repository.update(PROGRAMS, program_id, {"state": "published", "published_at": self.clock.now()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def update_program_guideline(self, context: AccessContext, program_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """更新计划当前指南;已立项项目仍使用冻结版本。"""
        context.require("write:portfolio")
        changes = {"guideline_ref": _text(values, "guideline_ref"), "guideline_version": _text(values, "guideline_version")}
        return self.repository.update(PROGRAMS, program_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def create_topic(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        program = self.repository.get(PROGRAMS, _text(values, "program_id"))
        payload = {"program_id": program["entity_id"], "code": _text(values, "code"), "title": _text(values, "title"), "state": "open"}
        return self.repository.create(TOPICS, payload, actor=context.actor_id, request_key=request_key)

    # ---- 项目与立项冻结 ----

    def create_project(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        topic = self.repository.get(TOPICS, _text(values, "topic_id"))
        lead_org = _text(values, "lead_org")
        self.repository.get(ORGANIZATIONS, lead_org)
        partners = []
        for org in values.get("partner_orgs", []) or []:
            if not isinstance(org, str) or not org.strip():
                raise ValidationError("partner_orgs 必须是单位标识列表")
            self.repository.get(ORGANIZATIONS, org.strip())
            if org.strip() not in partners:
                partners.append(org.strip())
        payload = {
            "topic_id": topic["entity_id"],
            "code": _text(values, "code"),
            "title": _text(values, "title"),
            "lead_org": lead_org,
            "partner_orgs": partners,
            "pi_id": _text(values, "pi_id"),
            "state": "draft",
        }
        return self.repository.create(PROJECTS, payload, actor=context.actor_id, request_key=request_key)

    def initiate_project(self, context: AccessContext, project_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """立项:冻结立项时适用的指南与预算版本。"""
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, project_id)
        if project["state"] != "draft":
            raise ConflictError("只有草稿项目可以立项")
        topic = self.repository.get(TOPICS, project["topic_id"])
        program = self.repository.get(PROGRAMS, topic["program_id"])
        if program["state"] != "published":
            raise ConflictError("计划指南尚未发布,不能立项")
        now = self.clock.now()
        changes = {
            "state": "initiated",
            "frozen_guideline": {"ref": program["guideline_ref"], "version": program["guideline_version"], "frozen_at": now},
            "frozen_budget_version": _text(values, "budget_version"),
            "initiated_at": now,
        }
        return self.repository.update(PROJECTS, project_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    # ---- 课题与里程碑 ----

    def create_task(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        lead_org = _text(values, "lead_org")
        self.repository.get(ORGANIZATIONS, lead_org)
        payload = {"project_id": project["entity_id"], "code": _text(values, "code"), "title": _text(values, "title"), "lead_org": lead_org, "state": "active"}
        return self.repository.create(TASKS, payload, actor=context.actor_id, request_key=request_key)

    def create_milestone(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        task = self.repository.get(TASKS, _text(values, "task_id"))
        seq = values.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValidationError("seq 必须是正整数")
        payload = {
            "task_id": task["entity_id"],
            "seq": seq,
            "title": _text(values, "title"),
            "due_at": canonical_instant(_text(values, "due_at")),
            "budget_minor": _money(values, "budget", allow_zero=True),
            "owner_id": _text(values, "owner_id"),
            "state": "pending",
        }
        return self.repository.create(MILESTONES, payload, actor=context.actor_id, request_key=request_key)

    def complete_milestone(self, context: AccessContext, milestone_id: str, *, expected_version: int, request_key: str) -> dict:
        context.require("write:portfolio")
        milestone = self.repository.get(MILESTONES, milestone_id)
        if milestone["state"] != "pending":
            raise ConflictError("里程碑不处于待办状态")
        return self.repository.update(MILESTONES, milestone_id, {"state": "completed", "completed_at": self.clock.now()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    # ---- 预算来源 ----

    def create_budget_source(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        channel = _text(values, "channel")
        if channel not in BUDGET_CHANNELS:
            raise ValidationError("未知预算来源渠道")
        payload = {
            "project_id": project["entity_id"],
            "channel": channel,
            "fiscal_year": _year(values),
            "amount_minor": _money(values, "amount"),
            "currency": _text(values, "currency") if "currency" in values else "CNY",
            "state": "registered",
        }
        return self.repository.create(BUDGET_SOURCES, payload, actor=context.actor_id, request_key=request_key)

    # ---- 设备资源与占用 ----

    def create_equipment(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        kind = _text(values, "kind")
        if kind not in EQUIPMENT_KINDS:
            raise ValidationError("未知设备类型")
        owner_org = _text(values, "owner_org")
        self.repository.get(ORGANIZATIONS, owner_org)
        capacity = values.get("capacity", 1)
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValidationError("capacity 必须是正整数")
        if kind in RESTRICTED_EQUIPMENT_KINDS and capacity != 1:
            raise ValidationError("受限样品与大型设备容量必须为一")
        payload = {"name": _text(values, "name"), "kind": kind, "owner_org": owner_org, "capacity": capacity, "state": "available"}
        return self.repository.create(EQUIPMENT, payload, actor=context.actor_id, request_key=request_key)

    def book_equipment(self, context: AccessContext, equipment_id: str, task_id: str, *, start_at: str, end_at: str) -> dict:
        """为课题占用设备;受限样品与大型设备容量为一,重叠占用会被拒绝。"""
        context.require("write:portfolio")
        equipment = self.repository.get(EQUIPMENT, equipment_id)
        task = self.repository.get(TASKS, task_id)
        self.require_project_scope(context, self.repository.get(PROJECTS, task["project_id"]))
        return self.reservations.reserve(resource_id=equipment["entity_id"], subject_id=task["entity_id"], quantity=1, capacity=int(equipment["capacity"]), start_at=start_at, end_at=end_at, actor=context.actor_id)

    # ---- 知识产权、技术合同与成果转化 ----

    def create_ip(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        payload = {"project_id": project["entity_id"], "kind": _text(values, "kind"), "title": _text(values, "title"), "registration_ref": _text(values, "registration_ref"), "state": "registered"}
        return self.repository.create(IP_RIGHTS, payload, actor=context.actor_id, request_key=request_key)

    def create_contract(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        amount_minor = _money(values, "amount")
        allocations = self._validate_allocations(values.get("allocations", []), amount_minor)
        payload = {
            "code": _text(values, "code"),
            "title": _text(values, "title"),
            "buyer_org": _text(values, "buyer_org"),
            "amount_minor": amount_minor,
            "currency": _text(values, "currency") if "currency" in values else "CNY",
            "signed_at": canonical_instant(_text(values, "signed_at")),
            "allocations": allocations,
            "state": "active",
        }
        return self.repository.create(CONTRACTS, payload, actor=context.actor_id, request_key=request_key)

    def allocate_contract(self, context: AccessContext, contract_id: str, additions: object, *, expected_version: int, request_key: str) -> dict:
        """追加合同收入归集;累计归集不能超过合同金额。"""
        context.require("write:portfolio")
        contract = self.repository.get(CONTRACTS, contract_id)
        combined = list(contract["allocations"]) + self._validate_allocations(additions, 0)
        if sum(item["amount_minor"] for item in combined) > contract["amount_minor"]:
            raise ValidationError("合同收入归集超过合同金额")
        return self.repository.update(CONTRACTS, contract_id, {"allocations": combined}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def record_transfer(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """成果转化:保存各项目贡献比例,比例之和必须等于一。"""
        context.require("write:portfolio")
        contract = self.repository.get(CONTRACTS, _text(values, "contract_id"))
        raw = values.get("contributions")
        if not isinstance(raw, list) or not raw:
            raise ValidationError("contributions 不能为空")
        seen: set[str] = set()
        contributions = []
        total = Decimal(0)
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationError("贡献比例条目必须是对象")
            project = self.repository.get(PROJECTS, _text(item, "project_id"))
            if project["entity_id"] in seen:
                raise ValidationError("同一项目不能重复记录贡献比例")
            seen.add(project["entity_id"])
            ratio = _ratio(item.get("ratio"))
            total += ratio
            contributions.append({"project_id": project["entity_id"], "ratio": str(ratio)})
        if total != Decimal(1):
            raise ValidationError("各项目贡献比例之和必须等于一")
        payload = {
            "contract_id": contract["entity_id"],
            "amount_minor": _money(values, "amount"),
            "fiscal_year": _year(values),
            "contributions": contributions,
            "state": "recorded",
        }
        return self.repository.create(TRANSFERS, payload, actor=context.actor_id, request_key=request_key)

    # ---- 资料授权 ----

    def share_material(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        self.require_project_scope(context, project)
        allowed = values.get("allowed_orgs")
        if not isinstance(allowed, list) or not allowed:
            raise ValidationError("allowed_orgs 必须是非空单位列表")
        orgs = []
        for org in allowed:
            if not isinstance(org, str) or not org.strip():
                raise ValidationError("allowed_orgs 必须是单位标识列表")
            self.repository.get(ORGANIZATIONS, org.strip())
            if org.strip() not in orgs:
                orgs.append(org.strip())
        payload = {"project_id": project["entity_id"], "title": _text(values, "title"), "kind": _text(values, "kind"), "allowed_orgs": orgs, "content_ref": _text(values, "content_ref"), "state": "shared"}
        return self.repository.create(MATERIALS, payload, actor=context.actor_id, request_key=request_key)

    def list_materials(self, context: AccessContext, project_id: str) -> list[dict]:
        context.require("read:portfolio")
        project = self.repository.get(PROJECTS, project_id)
        self.require_project_scope(context, project)
        rows = self.repository.search(MATERIALS, "project_id", project["entity_id"])
        return [row for row in rows if self._material_visible(context, row)]

    def get_material(self, context: AccessContext, material_id: str) -> dict:
        context.require("read:portfolio")
        material = self.repository.get(MATERIALS, material_id)
        if not self._material_visible(context, material):
            raise PermissionDenied("当前主体无权接触该资料")
        return material

    # ---- 重大调整:提交、审批、执行 ----

    def submit_adjustment(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        self.require_project_scope(context, project)
        kind = _text(values, "kind")
        if kind not in ADJUSTMENT_KINDS:
            raise ValidationError("未知调整类型")
        detail = values.get("detail")
        if not isinstance(detail, Mapping):
            raise ValidationError("detail 必须是对象")
        self._validate_adjustment_detail(project, kind, detail)
        payload = {"project_id": project["entity_id"], "kind": kind, "detail": dict(detail), "submitted_by": context.actor_id, "state": "submitted"}
        return self.repository.create(ADJUSTMENTS, payload, actor=context.actor_id, request_key=request_key)

    def approve_adjustment(self, context: AccessContext, adjustment_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        """审批重大调整;项目负责人不得审批自己项目的调整,审批人也不能是提交人。"""
        context.require("approve:portfolio")
        adjustment = self.repository.get(ADJUSTMENTS, adjustment_id)
        if adjustment["state"] != "submitted":
            raise ConflictError("调整不处于待审批状态")
        project = self.repository.get(PROJECTS, adjustment["project_id"])
        if context.actor_id == project["pi_id"]:
            raise PermissionDenied("项目负责人不得审批自己的重大调整")
        assert_distinct(adjustment["submitted_by"], context.actor_id)
        approve = bool(values.get("approve", True))
        reason = _text(values, "reason")
        changes = {"state": "approved" if approve else "rejected", "approver": context.actor_id, "decision_reason": reason, "decided_at": self.clock.now()}
        return self.repository.update(ADJUSTMENTS, adjustment_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def apply_adjustment(self, context: AccessContext, adjustment_id: str, *, request_key: str) -> dict:
        """执行已批准的调整;每一步幂等,中断后重入可继续,且只影响未完成阶段。"""
        context.require("write:portfolio")
        adjustment = self.repository.get(ADJUSTMENTS, adjustment_id)
        if adjustment["state"] == "applied":
            return adjustment
        if adjustment["state"] != "approved":
            raise ConflictError("调整尚未批准")
        kind = adjustment["kind"]
        detail = adjustment["detail"]
        project = self.repository.get(PROJECTS, adjustment["project_id"])
        if kind == "delay":
            self._apply_delay(context, detail, request_key)
        elif kind == "pi_change":
            self._apply_pi_change(context, project, detail, request_key)
        elif kind == "merge":
            self._apply_merge(context, project, detail, request_key)
        elif kind == "budget":
            self._apply_budget(context, project, detail, request_key)
        elif kind == "guideline":
            self._apply_guideline(context, project, request_key)
        current = self.repository.get(ADJUSTMENTS, adjustment_id)
        return self.repository.update(ADJUSTMENTS, adjustment_id, {"state": "applied", "applied_at": self.clock.now()}, actor=context.actor_id, expected_version=current["version"], request_key=request_key)

    # ---- 复核 ----

    def create_review(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        context.require("write:portfolio")
        sequence = values.get("sequence", 0)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValidationError("sequence 必须是非负整数")
        payload = {
            "subject": _text(values, "subject"),
            "source": _text(values, "source"),
            "source_key": _text(values, "source_key"),
            "sequence": sequence,
            "detail": dict(values.get("detail", {})) if isinstance(values.get("detail", {}), Mapping) else {},
            "state": "pending",
        }
        return self.repository.create(REVIEWS, payload, actor=context.actor_id, request_key=request_key)

    def list_reviews(self, context: AccessContext, *, state: str | None = None) -> list[dict]:
        context.require("read:portfolio")
        if not (context.allows("approve:portfolio") or context.allows("audit:portfolio")):
            raise PermissionDenied("只有复核人或审核人可以查看复核队列")
        if state is not None and state not in ("pending", "resolved"):
            raise ValidationError("未知复核状态")
        return self.repository.list(REVIEWS, state=state)

    def resolve_review(self, context: AccessContext, review_id: str, values: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        context.require("approve:portfolio")
        review = self.repository.get(REVIEWS, review_id)
        if review["state"] != "pending":
            raise ConflictError("复核已经处理")
        changes = {"state": "resolved", "resolution": _text(values, "resolution"), "resolved_by": context.actor_id, "resolved_at": self.clock.now()}
        return self.repository.update(REVIEWS, review_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    # ---- 查询 ----

    def get(self, context: AccessContext, entity_type: str, entity_id: str) -> dict:
        context.require("read:portfolio")
        record = self.repository.get(entity_type, entity_id)
        self._enforce_scope(context, entity_type, record)
        return record

    def list_tasks(self, context: AccessContext, project_id: str) -> list[dict]:
        context.require("read:portfolio")
        self.require_project_scope(context, self.repository.get(PROJECTS, project_id))
        return self.repository.search(TASKS, "project_id", project_id)

    def list_milestones(self, context: AccessContext, task_id: str) -> list[dict]:
        context.require("read:portfolio")
        task = self.repository.get(TASKS, task_id)
        self.require_project_scope(context, self.repository.get(PROJECTS, task["project_id"]))
        rows = self.repository.search(MILESTONES, "task_id", task["entity_id"])
        return sorted(rows, key=lambda row: (row["seq"], row["entity_id"]))

    # ---- 内部:调整执行 ----

    def _apply_delay(self, context: AccessContext, detail: Mapping[str, object], request_key: str) -> None:
        milestone = self.repository.get(MILESTONES, str(detail["milestone_id"]))
        if milestone["state"] != "pending":
            raise ConflictError("已完成的里程碑不能延期")
        new_due = canonical_instant(str(detail["new_due_at"]))
        if milestone["due_at"] == new_due:
            return
        self.repository.update(MILESTONES, milestone["entity_id"], {"due_at": new_due, "delayed": True}, actor=context.actor_id, expected_version=milestone["version"], request_key=f"{request_key}:ms:{milestone['entity_id']}")

    def _apply_pi_change(self, context: AccessContext, project: dict, detail: Mapping[str, object], request_key: str) -> None:
        new_pi = str(detail["new_pi_id"]).strip()
        if not new_pi:
            raise ValidationError("new_pi_id 不能为空")
        if project["pi_id"] != new_pi:
            self.repository.update(PROJECTS, project["entity_id"], {"pi_id": new_pi, "previous_pi_id": project["pi_id"]}, actor=context.actor_id, expected_version=project["version"], request_key=f"{request_key}:project")
        for task in self.repository.search(TASKS, "project_id", project["entity_id"]):
            for milestone in self.repository.search(MILESTONES, "task_id", task["entity_id"]):
                if milestone["state"] == "pending" and milestone["owner_id"] != new_pi:
                    self.repository.update(MILESTONES, milestone["entity_id"], {"owner_id": new_pi}, actor=context.actor_id, expected_version=milestone["version"], request_key=f"{request_key}:ms:{milestone['entity_id']}")

    def _apply_merge(self, context: AccessContext, project: dict, detail: Mapping[str, object], request_key: str) -> None:
        source = self.repository.get(TASKS, str(detail["source_task_id"]))
        target = self.repository.get(TASKS, str(detail["target_task_id"]))
        if source["state"] == "merged" and source.get("merged_into") == target["entity_id"]:
            return  # 合并已完成,中断重入直接结束
        if source["state"] != "active" or target["state"] != "active":
            raise ConflictError("合并双方课题必须都处于进行状态")
        for milestone in self.repository.search(MILESTONES, "task_id", source["entity_id"]):
            if milestone["state"] == "pending":
                self.repository.update(MILESTONES, milestone["entity_id"], {"task_id": target["entity_id"], "merged_from": source["entity_id"]}, actor=context.actor_id, expected_version=milestone["version"], request_key=f"{request_key}:ms:{milestone['entity_id']}")
        current = self.repository.get(TASKS, source["entity_id"])
        self.repository.update(TASKS, source["entity_id"], {"state": "merged", "merged_into": target["entity_id"]}, actor=context.actor_id, expected_version=current["version"], request_key=f"{request_key}:task:{source['entity_id']}")

    def _apply_budget(self, context: AccessContext, project: dict, detail: Mapping[str, object], request_key: str) -> None:
        new_version = str(detail["new_budget_version"]).strip()
        if not new_version:
            raise ValidationError("new_budget_version 不能为空")
        if project.get("frozen_budget_version") == new_version:
            return
        changes = {"previous_budget_version": project.get("frozen_budget_version"), "frozen_budget_version": new_version, "budget_refrozen_at": self.clock.now()}
        self.repository.update(PROJECTS, project["entity_id"], changes, actor=context.actor_id, expected_version=project["version"], request_key=f"{request_key}:project")

    def _apply_guideline(self, context: AccessContext, project: dict, request_key: str) -> None:
        topic = self.repository.get(TOPICS, project["topic_id"])
        program = self.repository.get(PROGRAMS, topic["program_id"])
        frozen = {"ref": program["guideline_ref"], "version": program["guideline_version"], "frozen_at": self.clock.now()}
        if project.get("frozen_guideline", {}).get("version") == frozen["version"] and project.get("frozen_guideline", {}).get("ref") == frozen["ref"]:
            return
        self.repository.update(PROJECTS, project["entity_id"], {"frozen_guideline": frozen}, actor=context.actor_id, expected_version=project["version"], request_key=f"{request_key}:project")

    # ---- 内部:校验与授权 ----

    def _validate_allocations(self, raw: object, amount_minor: int) -> list[dict]:
        if not isinstance(raw, list):
            raise ValidationError("allocations 必须是列表")
        allocations = []
        total = 0
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValidationError("归集条目必须是对象")
            task = self.repository.get(TASKS, _text(item, "task_id"))
            minor = _money(item, "amount")
            total += minor
            allocations.append({"task_id": task["entity_id"], "amount_minor": minor})
        if amount_minor and total > amount_minor:
            raise ValidationError("合同收入归集超过合同金额")
        return allocations

    def _validate_adjustment_detail(self, project: dict, kind: str, detail: Mapping[str, object]) -> None:
        if kind == "delay":
            milestone = self.repository.get(MILESTONES, _text(detail, "milestone_id"))
            task = self.repository.get(TASKS, milestone["task_id"])
            if task["project_id"] != project["entity_id"]:
                raise ValidationError("里程碑不属于该项目")
            canonical_instant(_text(detail, "new_due_at"))
        elif kind == "pi_change":
            _text(detail, "new_pi_id")
        elif kind == "merge":
            source = self.repository.get(TASKS, _text(detail, "source_task_id"))
            target = self.repository.get(TASKS, _text(detail, "target_task_id"))
            if source["entity_id"] == target["entity_id"]:
                raise ValidationError("不能合并到同一课题")
            if source["project_id"] != project["entity_id"] or target["project_id"] != project["entity_id"]:
                raise ValidationError("合并课题必须属于该项目")
        elif kind == "budget":
            _text(detail, "new_budget_version")

    def _org_ids(self, context: AccessContext) -> set[str]:
        return {scope.split(":", 1)[1] for scope in context.scopes if scope.startswith("org:")}

    def require_project_scope(self, context: AccessContext, project: dict) -> None:
        if context.has_scope("*") or context.allows("audit:portfolio"):
            return
        orgs = self._org_ids(context)
        if project["lead_org"] in orgs or any(org in orgs for org in project.get("partner_orgs", [])):
            return
        raise PermissionDenied("当前主体无权访问该项目")

    def _material_visible(self, context: AccessContext, material: dict) -> bool:
        if context.has_scope("*") or context.allows("audit:portfolio"):
            return True
        orgs = self._org_ids(context)
        return any(org in orgs for org in material.get("allowed_orgs", []))

    def _enforce_scope(self, context: AccessContext, entity_type: str, record: dict) -> None:
        if context.has_scope("*") or context.allows("audit:portfolio"):
            return
        if entity_type == PROJECTS:
            self.require_project_scope(context, record)
        elif entity_type == TASKS:
            self.require_project_scope(context, self.repository.get(PROJECTS, record["project_id"]))
        elif entity_type == MILESTONES:
            task = self.repository.get(TASKS, record["task_id"])
            self.require_project_scope(context, self.repository.get(PROJECTS, task["project_id"]))
        elif entity_type in (BUDGET_SOURCES, IP_RIGHTS):
            self.require_project_scope(context, self.repository.get(PROJECTS, record["project_id"]))
        elif entity_type == MATERIALS:
            if not self._material_visible(context, record):
                raise PermissionDenied("当前主体无权接触该资料")
        elif entity_type == EQUIPMENT:
            if record["owner_org"] not in self._org_ids(context):
                raise PermissionDenied("当前主体无权查看该设备")
        elif entity_type == CONTRACTS:
            orgs = self._org_ids(context)
            for allocation in record.get("allocations", []):
                task = self.repository.get(TASKS, allocation["task_id"])
                project = self.repository.get(PROJECTS, task["project_id"])
                if project["lead_org"] in orgs or any(org in orgs for org in project.get("partner_orgs", [])):
                    return
            raise PermissionDenied("当前主体无权查看该合同")
