"""研发组合资金、验收与可恢复任务。

- 拨款、配套资金、支出、支出退回都进入不可改写的资金分录,参考号唯一防重;
- 验收回执经收件箱接入:相同回执去重且不重复释放资金,内容冲突进入复核;
- 验收结算与预算结转是幂等任务,处理中断后凭分录参考号接续原任务;
- 审核人可从合同金额下钻到每笔资金、每次设施使用和相应责任人。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Mapping

from .errors import CivicFlowError, ConflictError, ValidationError
from .inbox import Inbox
from .jobs import JobQueue
from .ledger import Ledger, to_minor
from .portfolio import (
    BUDGET_CHANNELS,
    BUDGET_SOURCES,
    CONTRACTS,
    EQUIPMENT,
    MILESTONES,
    PROJECTS,
    TASKS,
    TRANSFERS,
    PortfolioService,
    _text,
    _year,
)
from .repository import EntityRepository
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant


ACCEPTANCES = "acceptances"
CARRYOVERS = "carryovers"
CURRENCY = "CNY"

SETTLE_JOB = "acceptance:settle"
CARRYOVER_JOB = "budget:carryover"


def journal_key(project_id: str, fiscal_year: int) -> str:
    """项目按年度分账,便于年度结转与下钻。"""
    return f"portfolio:{project_id}:{fiscal_year}"


def _minor_amount(minor: int) -> str:
    return format(Decimal(minor).scaleb(-2), "f")


@dataclass(frozen=True)
class PortfolioFunds:
    """研发组合资金分录、验收结算、预算结转与审计下钻。"""

    repository: EntityRepository
    ledger: Ledger
    inbox: Inbox
    jobs: JobQueue
    clock: Clock
    portfolio: PortfolioService

    # ---- 资金分录 ----

    def post_appropriation(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """拨款入账(借方),参考号唯一,重复入账会被拒绝。"""
        return self._post(context, values, account=f"appropriation:{_channel(values)}", direction="debit")

    def post_matching(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """配套资金入账(借方)。"""
        return self._post(context, values, account="matching", direction="debit")

    def post_expenditure(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """支出入账(贷方),不能超过该项目当年可用资金。"""
        project_id, fiscal_year, minor, currency, reference = self._entry_parts(values)
        self._authorize_project(context, project_id)
        journal = journal_key(project_id, fiscal_year)
        if self.ledger.balance(journal, currency=currency) - minor < 0:
            raise ValidationError("支出超过该项目当年可用资金")
        return self.ledger.post(journal_key=journal, account="expenditure", currency=currency, amount=_minor_amount(minor), direction="credit", reference=reference, actor=context.actor_id)

    def post_refund(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """支出退回入账(借方),累计退回不能超过累计支出。"""
        project_id, fiscal_year, minor, currency, reference = self._entry_parts(values)
        self._authorize_project(context, project_id)
        journal = journal_key(project_id, fiscal_year)
        spent = refunded = 0
        for entry in self.ledger.entries(journal):
            if entry["currency"] != currency:
                continue
            if entry["account"] == "expenditure" and entry["direction"] == "credit":
                spent += entry["amount_minor"]
            elif entry["account"] == "expenditure_refund" and entry["direction"] == "debit":
                refunded += entry["amount_minor"]
        if refunded + minor > spent:
            raise ValidationError("支出退回超过累计支出")
        return self.ledger.post(journal_key=journal, account="expenditure_refund", currency=currency, amount=_minor_amount(minor), direction="debit", reference=reference, actor=context.actor_id)

    def _post(self, context: AccessContext, values: Mapping[str, object], *, account: str, direction: str) -> dict:
        project_id, fiscal_year, minor, currency, reference = self._entry_parts(values)
        self._authorize_project(context, project_id)
        return self.ledger.post(journal_key=journal_key(project_id, fiscal_year), account=account, currency=currency, amount=_minor_amount(minor), direction=direction, reference=reference, actor=context.actor_id)

    def _entry_parts(self, values: Mapping[str, object]) -> tuple[str, int, int, str, str]:
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        fiscal_year = _year(values)
        minor = to_minor(str(values.get("amount", "")))
        if minor <= 0:
            raise ValidationError("金额必须大于零")
        currency = str(values.get("currency", CURRENCY)).strip() or CURRENCY
        reference = _text(values, "reference")
        return project["entity_id"], fiscal_year, minor, currency, reference

    def _authorize_project(self, context: AccessContext, project_id: str) -> None:
        context.require("write:portfolio")
        self.portfolio.require_project_scope(context, self.repository.get(PROJECTS, project_id))

    # ---- 验收回执 ----

    def receive_acceptance(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """接入验收回执:相同回执去重不重复释放,内容冲突进入复核。

        每一步都幂等,任何一点中断后重新提交同一回执即可继续。
        """
        context.require("write:portfolio")
        source = _text(values, "source")
        source_key = _text(values, "source_key")
        sequence = values.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValidationError("sequence 必须是非负整数")
        raw_payload = values.get("payload")
        if not isinstance(raw_payload, Mapping):
            raise ValidationError("payload 必须是对象")
        normalized = self._validate_acceptance_payload(raw_payload)
        occurred_at = canonical_instant(str(values["occurred_at"])) if "occurred_at" in values else self.clock.now()
        receipt_key = f"{source}:{source_key}:{sequence}"
        try:
            receipt = self.inbox.receive(source=source, source_key=source_key, sequence=sequence, payload=dict(raw_payload), occurred_at=occurred_at)
        except ConflictError:
            review = self.portfolio.create_review(context, {"subject": f"验收回执内容冲突 {receipt_key}", "source": source, "source_key": source_key, "sequence": sequence, "detail": {"incoming": dict(raw_payload)}}, request_key=f"review:{receipt_key}")
            return {"status": "review", "review_id": review["entity_id"]}
        acceptance = self._ensure_acceptance(context, receipt_key, source, source_key, sequence, normalized)
        self._ensure_settlement_job(context, acceptance)
        return {"status": receipt["status"], "acceptance_id": acceptance["entity_id"]}

    def _validate_acceptance_payload(self, payload: Mapping[str, object]) -> dict:
        project = self.repository.get(PROJECTS, _text(payload, "project_id"))
        releases = []
        for item in payload.get("releases", []) or []:
            if not isinstance(item, Mapping):
                raise ValidationError("释放条目必须是对象")
            milestone = self.repository.get(MILESTONES, _text(item, "milestone_id"))
            task = self.repository.get(TASKS, milestone["task_id"])
            if task["project_id"] != project["entity_id"]:
                raise ValidationError("释放的里程碑不属于该项目")
            minor = to_minor(str(item.get("amount", "")))
            if minor <= 0:
                raise ValidationError("释放金额必须大于零")
            releases.append({"milestone_id": milestone["entity_id"], "amount_minor": minor})
        if not releases:
            raise ValidationError("验收回执必须包含资金释放条目")
        return {
            "project_id": project["entity_id"],
            "fiscal_year": _year(payload),
            "currency": str(payload.get("currency", CURRENCY)).strip() or CURRENCY,
            "result": str(payload.get("result", "通过")).strip() or "通过",
            "releases": releases,
        }

    def _ensure_acceptance(self, context: AccessContext, receipt_key: str, source: str, source_key: str, sequence: int, normalized: dict) -> dict:
        found = self.repository.search(ACCEPTANCES, "receipt_key", receipt_key, limit=1)
        if found:
            return found[0]
        payload = {"receipt_key": receipt_key, "source": source, "source_key": source_key, "sequence": sequence, "state": "received", **normalized}
        return self.repository.create(ACCEPTANCES, payload, actor=context.actor_id, request_key=f"acceptance:{receipt_key}")

    def _ensure_settlement_job(self, context: AccessContext, acceptance: dict) -> None:
        if acceptance["state"] != "received":
            return
        active = [job for job in self.jobs.for_subject(acceptance["entity_id"]) if job["status"] in ("waiting", "running", "retry")]
        if active:
            return
        self.jobs.schedule(job_type=SETTLE_JOB, subject_id=acceptance["entity_id"], run_at=self.clock.now(), payload={"acceptance_id": acceptance["entity_id"], "actor": context.actor_id})

    # ---- 预算结转 ----

    def schedule_carryover(self, context: AccessContext, values: Mapping[str, object]) -> dict:
        """登记年度预算结转任务;同一项目同一年度只调度一次。"""
        context.require("write:portfolio")
        project = self.repository.get(PROJECTS, _text(values, "project_id"))
        from_year = _year(values, "from_year")
        to_year = _year(values, "to_year")
        if to_year <= from_year:
            raise ValidationError("结转目标年度必须晚于来源年度")
        currency = str(values.get("currency", CURRENCY)).strip() or CURRENCY
        subject = f"carryover:{project['entity_id']}:{from_year}"
        for job in self.jobs.for_subject(subject):
            if job["status"] in ("waiting", "running", "retry", "succeeded"):
                return {"job_id": job["job_id"], "replayed": True}
        job_id = self.jobs.schedule(job_type=CARRYOVER_JOB, subject_id=subject, run_at=self.clock.now(), payload={"project_id": project["entity_id"], "from_year": from_year, "to_year": to_year, "currency": currency, "actor": context.actor_id})
        return {"job_id": job_id, "replayed": False}

    # ---- 任务执行(可中断接续) ----

    def run_due_jobs(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        """执行到期的结算与结转任务;失败按退避重试,处理中断后凭分录参考号接续。"""
        context.require("write:portfolio")
        results = []
        for job in self.jobs.claim_due(limit=limit):
            payload = json.loads(job["payload_json"])
            try:
                if job["job_type"] == SETTLE_JOB:
                    self._settle_acceptance(payload)
                elif job["job_type"] == CARRYOVER_JOB:
                    self._carryover(payload)
                else:
                    raise ValidationError(f"未知任务类型 {job['job_type']}")
            except CivicFlowError as exc:
                delay = min(600, 30 * max(1, int(job["attempt"])))
                retry_at = (parse_instant(self.clock.now()) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
                self.jobs.retry(job["job_id"], error=str(exc), retry_at=retry_at)
                results.append({"job_id": job["job_id"], "job_type": job["job_type"], "status": "retry", "error": str(exc)})
            else:
                self.jobs.finish(job["job_id"])
                results.append({"job_id": job["job_id"], "job_type": job["job_type"], "status": "succeeded"})
        return results

    def _settle_acceptance(self, payload: Mapping[str, object]) -> None:
        acceptance = self.repository.get(ACCEPTANCES, str(payload["acceptance_id"]))
        if acceptance["state"] == "settled":
            return
        actor = str(payload.get("actor", "system"))
        journal = journal_key(acceptance["project_id"], int(acceptance["fiscal_year"]))
        for index, release in enumerate(acceptance["releases"]):
            try:
                self.ledger.post(journal_key=journal, account="release", currency=acceptance["currency"], amount=_minor_amount(int(release["amount_minor"])), direction="debit", reference=f"accept:{acceptance['entity_id']}:{index}", actor=actor)
            except ConflictError:
                continue  # 该笔已释放,重复回执或中断重入都不会再次入账
        current = self.repository.get(ACCEPTANCES, acceptance["entity_id"])
        self.repository.update(ACCEPTANCES, acceptance["entity_id"], {"state": "settled", "settled_at": self.clock.now()}, actor=actor, expected_version=current["version"], request_key=f"settle:{acceptance['entity_id']}")
        self._maybe_accept_project(acceptance["project_id"], actor)

    def _maybe_accept_project(self, project_id: str, actor: str) -> None:
        project = self.repository.get(PROJECTS, project_id)
        if project["state"] != "initiated":
            return
        for task in self.repository.search(TASKS, "project_id", project_id):
            for milestone in self.repository.search(MILESTONES, "task_id", task["entity_id"]):
                if milestone["state"] != "completed":
                    return
        self.repository.update(PROJECTS, project_id, {"state": "accepted", "accepted_at": self.clock.now()}, actor=actor, expected_version=project["version"], request_key=f"project-accepted:{project_id}")

    def _carryover(self, payload: Mapping[str, object]) -> None:
        project_id = str(payload["project_id"])
        from_year = int(payload["from_year"])
        to_year = int(payload["to_year"])
        currency = str(payload.get("currency", CURRENCY))
        actor = str(payload.get("actor", "system"))
        source = journal_key(project_id, from_year)
        out_reference = f"carryover:{project_id}:{from_year}:out"
        out_entry = next((entry for entry in self.ledger.entries(source) if entry["reference"] == out_reference), None)
        if out_entry is not None:
            carried = int(out_entry["amount_minor"])
        else:
            balance = self.ledger.balance(source, currency=currency)
            carried = 0
            if balance > 0:
                try:
                    self.ledger.post(journal_key=source, account="carryout", currency=currency, amount=_minor_amount(balance), direction="credit", reference=out_reference, actor=actor)
                    carried = balance
                except ConflictError:
                    out_entry = next(entry for entry in self.ledger.entries(source) if entry["reference"] == out_reference)
                    carried = int(out_entry["amount_minor"])
        if carried > 0:
            try:
                self.ledger.post(journal_key=journal_key(project_id, to_year), account="carryin", currency=currency, amount=_minor_amount(carried), direction="debit", reference=f"carryover:{project_id}:{from_year}:in", actor=actor)
            except ConflictError:
                pass  # 转入分录已存在,中断重入直接跳过
        found = self.repository.search(CARRYOVERS, "carryover_key", f"{project_id}:{from_year}", limit=1)
        if not found:
            self.repository.create(CARRYOVERS, {"carryover_key": f"{project_id}:{from_year}", "project_id": project_id, "from_year": from_year, "to_year": to_year, "currency": currency, "amount_minor": carried, "state": "posted"}, actor=actor, request_key=f"carryover:{project_id}:{from_year}")

    # ---- 成果转化收入分配 ----

    def distribute_transfer_income(self, context: AccessContext, transfer_id: str) -> dict:
        """按贡献比例把转化收入记入各项目账簿;重复执行不会重复入账。"""
        context.require("write:portfolio")
        transfer = self.repository.get(TRANSFERS, transfer_id)
        contributions = transfer["contributions"]
        shares = [int((Decimal(transfer["amount_minor"]) * Decimal(item["ratio"])).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)) for item in contributions]
        remainder = transfer["amount_minor"] - sum(shares)
        if remainder:
            largest = max(range(len(shares)), key=lambda index: shares[index])
            shares[largest] += remainder
        posted = []
        for item, share in zip(contributions, shares):
            if share <= 0:
                continue
            reference = f"transfer:{transfer['entity_id']}:{item['project_id']}"
            try:
                entry = self.ledger.post(journal_key=journal_key(item["project_id"], int(transfer["fiscal_year"])), account="transfer_income", currency=CURRENCY, amount=_minor_amount(share), direction="debit", reference=reference, actor=context.actor_id)
                posted.append({"project_id": item["project_id"], "amount_minor": share, "entry_id": entry["entry_id"]})
            except ConflictError:
                posted.append({"project_id": item["project_id"], "amount_minor": share, "replayed": True})
        return {"transfer_id": transfer["entity_id"], "shares": posted}

    # ---- 审核下钻 ----

    def project_budget_view(self, context: AccessContext, project_id: str) -> dict:
        """项目资金视图:分年度余额与未完成里程碑占用预算。"""
        context.require("read:portfolio")
        project = self.repository.get(PROJECTS, project_id)
        self.portfolio.require_project_scope(context, project)
        journals = []
        for key in self.ledger.journals(prefix=f"portfolio:{project_id}:"):
            journals.append({"journal_key": key, "balance_minor": self.ledger.balance(key, currency=CURRENCY)})
        pending = 0
        for task in self.repository.search(TASKS, "project_id", project_id):
            for milestone in self.repository.search(MILESTONES, "task_id", task["entity_id"]):
                if milestone["state"] == "pending":
                    pending += int(milestone["budget_minor"])
        return {"project_id": project_id, "journals": journals, "pending_milestone_budget_minor": pending}

    def contract_drilldown(self, context: AccessContext, contract_id: str) -> dict:
        """从合同金额下钻到每笔资金分录、每次设施使用和相应责任人。"""
        context.require("audit:portfolio")
        contract = self.repository.get(CONTRACTS, contract_id)
        tasks = []
        projects: dict[str, dict] = {}
        for allocation in contract["allocations"]:
            task = self.repository.get(TASKS, allocation["task_id"])
            project = self.repository.get(PROJECTS, task["project_id"])
            tasks.append({"task_id": task["entity_id"], "code": task["code"], "title": task["title"], "project_id": project["entity_id"], "amount_minor": allocation["amount_minor"]})
            if project["entity_id"] not in projects:
                projects[project["entity_id"]] = self._project_drilldown(project)
        transfers = self.repository.search(TRANSFERS, "contract_id", contract_id)
        allocated = sum(item["amount_minor"] for item in contract["allocations"])
        return {
            "contract": contract,
            "allocated_minor": allocated,
            "unallocated_minor": contract["amount_minor"] - allocated,
            "tasks": tasks,
            "projects": list(projects.values()),
            "transfers": transfers,
        }

    def _project_drilldown(self, project: dict) -> dict:
        journals = []
        for key in self.ledger.journals(prefix=f"portfolio:{project['entity_id']}:"):
            entries = self.ledger.entries(key)
            journals.append({"journal_key": key, "balance_minor": self.ledger.balance(key, currency=CURRENCY), "entries": entries})
        milestones = []
        equipment_usage = []
        pending = 0
        for task in self.repository.search(TASKS, "project_id", project["entity_id"]):
            for milestone in self.repository.search(MILESTONES, "task_id", task["entity_id"]):
                milestones.append(milestone)
                if milestone["state"] == "pending":
                    pending += int(milestone["budget_minor"])
            for reservation in self.portfolio.reservations.list_for_subject(task["entity_id"]):
                equipment = self.repository.get(EQUIPMENT, reservation["resource_id"])
                equipment_usage.append({**reservation, "equipment_name": equipment["name"], "equipment_kind": equipment["kind"], "task_id": task["entity_id"]})
        milestones.sort(key=lambda row: (row["task_id"], row["seq"], row["entity_id"]))
        return {
            "project_id": project["entity_id"],
            "code": project["code"],
            "title": project["title"],
            "pi_id": project["pi_id"],
            "lead_org": project["lead_org"],
            "frozen_guideline": project.get("frozen_guideline"),
            "frozen_budget_version": project.get("frozen_budget_version"),
            "budget_sources": self.repository.search(BUDGET_SOURCES, "project_id", project["entity_id"]),
            "journals": journals,
            "milestones": milestones,
            "pending_milestone_budget_minor": pending,
            "equipment_usage": equipment_usage,
        }


def _channel(values: Mapping[str, object]) -> str:
    channel = _text(values, "channel")
    if channel not in BUDGET_CHANNELS:
        raise ValidationError("未知预算来源渠道")
    return channel
