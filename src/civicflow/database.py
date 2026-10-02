"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);
-- 研发组合管理：层级单元与冻结版本
CREATE TABLE IF NOT EXISTS pf_guidelines (
    guideline_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY(guideline_id, version)
);
CREATE TABLE IF NOT EXISTS pf_budgets (
    budget_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    unit_type TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    carried_from TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY(budget_id, version)
);
CREATE INDEX IF NOT EXISTS pf_budgets_unit ON pf_budgets(unit_type, unit_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS pf_budgets_carryover ON pf_budgets(carried_from) WHERE carried_from IS NOT NULL;
CREATE TABLE IF NOT EXISTS pf_budget_lines (
    budget_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    topic_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    PRIMARY KEY(budget_id, version, topic_id, source_kind, source_ref)
);
CREATE TABLE IF NOT EXISTS pf_units (
    unit_type TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    leader TEXT,
    state TEXT NOT NULL,
    guideline_id TEXT,
    guideline_version INTEGER,
    budget_id TEXT,
    budget_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY(unit_type, unit_id)
);
CREATE INDEX IF NOT EXISTS pf_units_parent ON pf_units(parent_id);
CREATE TABLE IF NOT EXISTS pf_participants (
    participant_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    role TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    UNIQUE(org_id, unit_type, unit_id)
);
-- 里程碑（阶段）与预算占用
CREATE TABLE IF NOT EXISTS pf_milestones (
    milestone_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    leader TEXT NOT NULL,
    due_at TEXT NOT NULL,
    hold_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pf_milestones_topic ON pf_milestones(topic_id, state);
-- 不可改写资金分录
CREATE TABLE IF NOT EXISTS pf_fund_entries (
    entry_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_kind TEXT,
    source_ref TEXT,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    reference TEXT NOT NULL,
    milestone_id TEXT,
    contract_id TEXT,
    receipt_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS pf_fund_dedup ON pf_fund_entries(topic_id, kind, reference);
CREATE INDEX IF NOT EXISTS pf_fund_topic ON pf_fund_entries(topic_id, occurred_at);
CREATE INDEX IF NOT EXISTS pf_fund_contract ON pf_fund_entries(contract_id);
CREATE INDEX IF NOT EXISTS pf_fund_milestone ON pf_fund_entries(milestone_id);
-- 受限样品/大型设备专享占用
CREATE TABLE IF NOT EXISTS pf_occupancies (
    occupancy_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pf_occ_window ON pf_occupancies(asset_kind, asset_id, start_at, end_at, status);
-- 共享平台设施使用与计费
CREATE TABLE IF NOT EXISTS pf_facility_usage (
    usage_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    charge_minor INTEGER NOT NULL,
    charge_reference TEXT NOT NULL,
    charged_entry_id TEXT,
    used_by TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(facility_id, charge_reference)
);
CREATE INDEX IF NOT EXISTS pf_usage_topic ON pf_facility_usage(topic_id);
-- 成果资料与授权
CREATE TABLE IF NOT EXISTS pf_materials (
    material_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    title TEXT NOT NULL,
    classification TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pf_material_grants (
    grant_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    UNIQUE(material_id, org_id)
);
-- 知识产权、技术合同与贡献比例
CREATE TABLE IF NOT EXISTS pf_ips (
    ip_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pf_contracts (
    contract_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pf_contract_refs (
    contract_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    PRIMARY KEY(contract_id, ref_kind, ref_id)
);
CREATE TABLE IF NOT EXISTS pf_contract_topics (
    contract_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    share_bp INTEGER NOT NULL,
    PRIMARY KEY(contract_id, topic_id)
);
CREATE TABLE IF NOT EXISTS pf_contributions (
    result_type TEXT NOT NULL,
    result_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    share_bp INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(result_type, result_id, project_id)
);
-- 批准链
CREATE TABLE IF NOT EXISTS pf_approvals (
    approval_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_id, step)
);
CREATE INDEX IF NOT EXISTS pf_approvals_subject ON pf_approvals(subject_type, subject_id, step);
-- 重大调整（延期、负责人变更、任务合并）
CREATE TABLE IF NOT EXISTS pf_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    applicant TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
-- 验收回执（重复回执不得再次释放资金）
CREATE TABLE IF NOT EXISTS pf_acceptances (
    receipt_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    state TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    release_entry_id TEXT
);
-- 内容冲突复核
CREATE TABLE IF NOT EXISTS pf_conflicts (
    conflict_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
