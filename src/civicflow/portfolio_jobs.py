"""研发组合可恢复工作流：预算结转与验收处理在中断后可接续原任务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .errors import CivicFlowError, NotFoundError
from .jobs import JobQueue
from .portfolio import PortfolioBook
from .timeutil import parse_instant

JOB_CARRYOVER = "portfolio.carryover"
JOB_ACCEPTANCE = "portfolio.acceptance"


@dataclass(frozen=True)
class PortfolioWorkflows:
    """把组合登记册的动作挂到可恢复任务队列上。

    每个任务都以业务键幂等：结转以请求键作为 carried_from（部分唯一索引），
    验收以回执号去重。任务在任意一步中断，租约过期后被重新领取时只会接续，
    不会重复冻结预算或重复释放资金。
    """

    book: PortfolioBook
    jobs: JobQueue

    # ----- 预算结转 -----

    def request_carryover(self, *, unit_type: str, unit_id: str, currency: str, lines: list[dict], actor: str, request_key: str, run_at: str | None = None) -> dict:
        payload = {"unit_type": unit_type, "unit_id": unit_id, "currency": currency, "lines": lines, "actor": actor, "request_key": request_key}
        job_id = self.jobs.schedule(job_type=JOB_CARRYOVER, subject_id=f"{unit_type}:{unit_id}", run_at=run_at or self.book.clock.now(), payload=payload)
        return {"job_id": job_id, "request_key": request_key, "state": "scheduled"}

    def carry_over_now(self, *, unit_type: str, unit_id: str, currency: str, lines: list[dict], actor: str, request_key: str) -> dict:
        """同步执行结转；同一 request_key 重放返回首次冻结的版本。"""
        existing = self.book.budget_by_carry_key(request_key)
        if existing is not None:
            existing["replayed"] = True
            return existing
        return self.book.freeze_budget(unit_type=unit_type, unit_id=unit_id, currency=currency, lines=lines, carried_from=request_key, actor=actor)

    # ----- 验收处理 -----

    def queue_acceptance(self, *, milestone_id: str, receipt_id: str, amount: str, run_at: str | None = None) -> dict:
        job_id = self.jobs.schedule(job_type=JOB_ACCEPTANCE, subject_id=milestone_id, run_at=run_at or self.book.clock.now(), payload={"milestone_id": milestone_id, "receipt_id": receipt_id, "amount": amount})
        return {"job_id": job_id, "receipt_id": receipt_id, "state": "scheduled"}

    def run_due(self, *, limit: int = 10) -> list[dict]:
        """领取并执行到期任务；失败任务按退避重试，超过上限进 failed，不影响其他任务。"""
        claimed = self.jobs.claim_due(limit=limit)
        results: list[dict] = []
        for job in claimed:
            outcome = self._dispatch(job)
            results.append(outcome)
        return results

    def _dispatch(self, job: dict) -> dict:
        payload = job["payload_json"] if isinstance(job["payload_json"], dict) else __import__("json").loads(job["payload_json"])
        try:
            if job["job_type"] == JOB_CARRYOVER:
                output = self.carry_over_now(unit_type=payload["unit_type"], unit_id=payload["unit_id"], currency=payload["currency"], lines=payload["lines"], actor=payload["actor"], request_key=payload["request_key"])
            elif job["job_type"] == JOB_ACCEPTANCE:
                # 崩溃可能发生在回执登记之前：缺回执则先补登记，处理本身按回执幂等
                try:
                    output = self.book.process_acceptance(payload["receipt_id"], actor="portfolio-worker")
                except NotFoundError:
                    self.book.receive_acceptance(milestone_id=payload["milestone_id"], receipt_id=payload["receipt_id"], amount=payload["amount"])
                    output = self.book.process_acceptance(payload["receipt_id"], actor="portfolio-worker")
            else:
                raise CivicFlowError(f"未知任务类型 {job['job_type']}")
            self.jobs.finish(job["job_id"])
            return {"job_id": job["job_id"], "job_type": job["job_type"], "state": "succeeded", "output": output}
        except CivicFlowError as exc:
            retry_at = (parse_instant(self.book.clock.now()) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
            self.jobs.retry(job["job_id"], error=str(exc), retry_at=retry_at)
            return {"job_id": job["job_id"], "job_type": job["job_type"], "state": "retry", "error": str(exc)}
