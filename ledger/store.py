"""SQLite 存储层：建表、读写与查询映射。

这一层只负责“把对象存进去、取出来”，不做业务判断；状态机、权限与结算规则
都在 services 层。这样替换存储时不用重写规则，测试里也可以直接塞初始数据。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Iterator, Optional

from .models import (
    AuditEntry,
    Budget,
    Category,
    Correction,
    CorrectionStatus,
    Direction,
    Ledger,
    LedgerType,
    Membership,
    Notification,
    RecordState,
    RoleType,
    Settlement,
    SettlementSnapshot,
    TransactionRecord,
    User,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    nickname    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledgers (
    ledger_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    membership_id TEXT PRIMARY KEY,
    ledger_id     TEXT NOT NULL REFERENCES ledgers(ledger_id),
    user_id       TEXT NOT NULL REFERENCES users(user_id),
    role          TEXT NOT NULL,
    joined_at     TEXT NOT NULL,
    left_at       TEXT,
    UNIQUE (ledger_id, user_id)
);

CREATE TABLE IF NOT EXISTS categories (
    category_id TEXT PRIMARY KEY,
    ledger_id   TEXT NOT NULL REFERENCES ledgers(ledger_id),
    name        TEXT NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ledger_id, name)
);

CREATE TABLE IF NOT EXISTS records (
    record_id     TEXT PRIMARY KEY,
    ledger_id     TEXT NOT NULL REFERENCES ledgers(ledger_id),
    user_id       TEXT NOT NULL REFERENCES users(user_id),
    direction     TEXT NOT NULL,
    amount        TEXT NOT NULL,
    occurred_on   TEXT NOT NULL,
    category_id   TEXT NOT NULL REFERENCES categories(category_id),
    state         TEXT NOT NULL,
    txn_no        TEXT,
    operation_id  TEXT,
    remark        TEXT NOT NULL DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 1,
    replaced_by   TEXT,
    void_reason   TEXT,
    reject_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_ledger ON records(ledger_id, state);
CREATE INDEX IF NOT EXISTS idx_records_period ON records(ledger_id, occurred_on);
CREATE INDEX IF NOT EXISTS idx_records_effective
    ON records(ledger_id, state, occurred_on, category_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_op
    ON records(ledger_id, operation_id) WHERE operation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    record_id     TEXT NOT NULL REFERENCES records(record_id),
    proposer_id   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    old_amount    TEXT NOT NULL,
    new_amount    TEXT NOT NULL,
    new_occurred  TEXT NOT NULL,
    status        TEXT NOT NULL,
    reviewer_id   TEXT,
    review_remark TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id        TEXT PRIMARY KEY,
    ledger_id        TEXT NOT NULL REFERENCES ledgers(ledger_id),
    period           TEXT NOT NULL,
    amount           TEXT NOT NULL,
    overrun_notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ledger_id, period)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    ledger_id     TEXT NOT NULL REFERENCES ledgers(ledger_id),
    period        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    locked        INTEGER NOT NULL DEFAULT 1,
    version       INTEGER NOT NULL DEFAULT 1,
    operator_id   TEXT NOT NULL,
    reopened_by   TEXT,
    reopen_reason TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settle_ledger ON settlements(ledger_id, period);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id      TEXT PRIMARY KEY,
    ledger_id   TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    result      TEXT NOT NULL,
    before      TEXT NOT NULL DEFAULT '',
    after       TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    ledger_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    body            TEXT NOT NULL,
    delivered_at    TEXT NOT NULL,
    read            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invites (
    token       TEXT PRIMARY KEY,
    ledger_id   TEXT NOT NULL REFERENCES ledgers(ledger_id),
    inviter_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_by     TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _d(value: str) -> date:
    return date.fromisoformat(value)


class Store:
    """对 sqlite3 的薄封装，统一事务与行映射。"""

    def __init__(self, path: str = ":memory:", init: bool = True) -> None:
        """init=False 用于已经建过库的连接，避免每个请求都跑一遍建表脚本。"""
        self.conn = sqlite3.connect(path, detect_types=0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # WAL 让读操作不阻塞其他连接，并发查询时才不会互相排队
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        if init:
            self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- 事务
    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- 用户
    def add_user(self, user: User) -> None:
        self.conn.execute(
            "INSERT INTO users (user_id, email, nickname, created_at) VALUES (?,?,?,?)",
            (user.user_id, user.email, user.nickname, user.created_at.isoformat()),
        )

    def get_user(self, user_id: str) -> Optional[User]:
        row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        return User(row["user_id"], row["email"], row["nickname"], _dt(row["created_at"]))

    def find_user_by_email(self, email: str) -> Optional[User]:
        row = self.conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
        return None if row is None else self.get_user(row["user_id"])

    def list_users(self) -> list[User]:
        rows = self.conn.execute("SELECT user_id FROM users ORDER BY created_at").fetchall()
        return [self.get_user(r["user_id"]) for r in rows]  # type: ignore[misc]

    # ---------------------------------------------------------------- 账本
    def add_ledger(self, ledger: Ledger) -> None:
        self.conn.execute(
            "INSERT INTO ledgers (ledger_id, name, type, archived, created_at) VALUES (?,?,?,?,?)",
            (ledger.ledger_id, ledger.name, ledger.type.value,
             int(ledger.archived), ledger.created_at.isoformat()),
        )

    def update_ledger(self, ledger: Ledger) -> None:
        self.conn.execute(
            "UPDATE ledgers SET name=?, archived=? WHERE ledger_id=?",
            (ledger.name, int(ledger.archived), ledger.ledger_id),
        )

    def get_ledger(self, ledger_id: str) -> Optional[Ledger]:
        row = self.conn.execute("SELECT * FROM ledgers WHERE ledger_id=?", (ledger_id,)).fetchone()
        if row is None:
            return None
        return Ledger(row["ledger_id"], row["name"], LedgerType(row["type"]),
                      bool(row["archived"]), _dt(row["created_at"]))

    def list_ledgers(self) -> list[Ledger]:
        rows = self.conn.execute("SELECT ledger_id FROM ledgers ORDER BY created_at").fetchall()
        return [self.get_ledger(r["ledger_id"]) for r in rows]  # type: ignore[misc]

    # ---------------------------------------------------------------- 成员
    def add_membership(self, m: Membership) -> None:
        self.conn.execute(
            "INSERT INTO memberships (membership_id, ledger_id, user_id, role, joined_at, left_at)"
            " VALUES (?,?,?,?,?,?)",
            (m.membership_id, m.ledger_id, m.user_id, m.role.value,
             m.joined_at.isoformat(), m.left_at.isoformat() if m.left_at else None),
        )

    def update_membership(self, m: Membership) -> None:
        self.conn.execute(
            "UPDATE memberships SET role=?, left_at=? WHERE membership_id=?",
            (m.role.value, m.left_at.isoformat() if m.left_at else None, m.membership_id),
        )

    def get_membership(self, ledger_id: str, user_id: str) -> Optional[Membership]:
        row = self.conn.execute(
            "SELECT * FROM memberships WHERE ledger_id=? AND user_id=? AND left_at IS NULL",
            (ledger_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return Membership(row["membership_id"], row["ledger_id"], row["user_id"],
                          RoleType(row["role"]), _dt(row["joined_at"]),
                          _dt(row["left_at"]) if row["left_at"] else None)

    def get_membership_any(self, ledger_id: str, user_id: str) -> Optional[Membership]:
        """包含已退出的历史关系，用于“退出后再邀请”时复用同一行。"""
        row = self.conn.execute(
            "SELECT * FROM memberships WHERE ledger_id=? AND user_id=?",
            (ledger_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return Membership(row["membership_id"], row["ledger_id"], row["user_id"],
                          RoleType(row["role"]), _dt(row["joined_at"]),
                          _dt(row["left_at"]) if row["left_at"] else None)

    def list_memberships(self, ledger_id: str, active_only: bool = True) -> list[Membership]:
        sql = "SELECT * FROM memberships WHERE ledger_id=?"
        if active_only:
            sql += " AND left_at IS NULL"
        rows = self.conn.execute(sql + " ORDER BY joined_at", (ledger_id,)).fetchall()
        return [Membership(r["membership_id"], r["ledger_id"], r["user_id"], RoleType(r["role"]),
                           _dt(r["joined_at"]), _dt(r["left_at"]) if r["left_at"] else None)
                for r in rows]

    # ---------------------------------------------------------------- 类别
    def add_category(self, c: Category) -> None:
        self.conn.execute(
            "INSERT INTO categories (category_id, ledger_id, name, disabled) VALUES (?,?,?,?)",
            (c.category_id, c.ledger_id, c.name, int(c.disabled)),
        )

    def get_category(self, category_id: str) -> Optional[Category]:
        row = self.conn.execute(
            "SELECT * FROM categories WHERE category_id=?", (category_id,)
        ).fetchone()
        if row is None:
            return None
        return Category(row["category_id"], row["ledger_id"], row["name"], bool(row["disabled"]))

    def find_category(self, ledger_id: str, name: str) -> Optional[Category]:
        row = self.conn.execute(
            "SELECT category_id FROM categories WHERE ledger_id=? AND name=?", (ledger_id, name)
        ).fetchone()
        return None if row is None else self.get_category(row["category_id"])

    def list_categories(self, ledger_id: str) -> list[Category]:
        rows = self.conn.execute(
            "SELECT * FROM categories WHERE ledger_id=? ORDER BY name", (ledger_id,)
        ).fetchall()
        return [Category(r["category_id"], r["ledger_id"], r["name"],
                         bool(r["disabled"])) for r in rows]

    def disable_category(self, category_id: str) -> None:
        self.conn.execute("UPDATE categories SET disabled=1 WHERE category_id=?", (category_id,))

    # ---------------------------------------------------------------- 记录
    def add_record(self, rec: TransactionRecord) -> None:
        self.conn.execute(
            "INSERT INTO records (record_id, ledger_id, user_id, direction, amount, occurred_on,"
            " category_id, state, txn_no, operation_id, remark, version, replaced_by, void_reason,"
            " reject_reason, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.record_id, rec.ledger_id, rec.user_id, rec.direction.value, str(rec.amount),
             rec.occurred_on.isoformat(), rec.category_id, rec.state.value, rec.txn_no,
             rec.operation_id, rec.remark, rec.version, rec.replaced_by, rec.void_reason,
             rec.reject_reason, rec.created_at.isoformat(), rec.updated_at.isoformat()),
        )

    def update_record(self, rec: TransactionRecord) -> None:
        self.conn.execute(
            "UPDATE records SET state=?, direction=?, amount=?, occurred_on=?, category_id=?,"
            " txn_no=?, operation_id=?, version=?, replaced_by=?, void_reason=?,"
            " reject_reason=?, remark=?, updated_at=? WHERE record_id=?",
            (rec.state.value, rec.direction.value, str(rec.amount), rec.occurred_on.isoformat(),
             rec.category_id, rec.txn_no, rec.operation_id, rec.version, rec.replaced_by,
             rec.void_reason, rec.reject_reason, rec.remark, rec.updated_at.isoformat(),
             rec.record_id),
        )

    def _row_to_record(self, row: sqlite3.Row) -> TransactionRecord:
        return TransactionRecord(
            record_id=row["record_id"], ledger_id=row["ledger_id"], user_id=row["user_id"],
            direction=Direction(row["direction"]), amount=Decimal(row["amount"]),
            occurred_on=_d(row["occurred_on"]), category_id=row["category_id"],
            state=RecordState(row["state"]), txn_no=row["txn_no"], operation_id=row["operation_id"],
            remark=row["remark"], version=row["version"], replaced_by=row["replaced_by"],
            void_reason=row["void_reason"], reject_reason=row["reject_reason"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def get_record(self, record_id: str) -> Optional[TransactionRecord]:
        row = self.conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        return None if row is None else self._row_to_record(row)

    def find_by_operation(self, ledger_id: str, operation_id: str) -> Optional[TransactionRecord]:
        row = self.conn.execute(
            "SELECT * FROM records WHERE ledger_id=? AND operation_id=?", (ledger_id, operation_id)
        ).fetchone()
        return None if row is None else self._row_to_record(row)

    def find_by_txn_no(self, ledger_id: str, txn_no: str) -> list[TransactionRecord]:
        """交易号可能被作废记录占用过，因此返回列表由调用方按状态判断。"""
        rows = self.conn.execute(
            "SELECT * FROM records WHERE ledger_id=? AND txn_no=?", (ledger_id, txn_no)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_records(self, ledger_id: str, states: Optional[Iterable[RecordState]] = None
                     ) -> list[TransactionRecord]:
        sql = "SELECT * FROM records WHERE ledger_id=?"
        params: list[object] = [ledger_id]
        if states is not None:
            values = [s.value for s in states]
            sql += f" AND state IN ({','.join('?' * len(values))})"
            params.extend(values)
        rows = self.conn.execute(sql + " ORDER BY occurred_on, created_at", params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def query_records(self, ledger_id: str, *, category_id: Optional[str] = None,
                      member_id: Optional[str] = None, start: Optional[date] = None,
                      end: Optional[date] = None, min_amount: Optional[Decimal] = None,
                      max_amount: Optional[Decimal] = None,
                      effective_only: bool = True) -> list[TransactionRecord]:
        """条件查询下推到 SQL。

        早期版本把整本账读进内存再过滤，一万条记录下 20 个并发查询会退化成
        几十秒，因此这里改成 WHERE 条件，只把命中的行读出来。
        """
        sql = ["SELECT * FROM records WHERE ledger_id=?"]
        params: list[object] = [ledger_id]
        if effective_only:
            sql.append("AND state='approved' AND void_reason IS NULL AND replaced_by IS NULL")
        if category_id:
            sql.append("AND category_id=?")
            params.append(category_id)
        if member_id:
            sql.append("AND user_id=?")
            params.append(member_id)
        if start:
            sql.append("AND occurred_on >= ?")
            params.append(start.isoformat())
        if end:
            sql.append("AND occurred_on <= ?")
            params.append(end.isoformat())
        if min_amount is not None:
            sql.append("AND CAST(amount AS REAL) >= ?")
            params.append(float(min_amount))
        if max_amount is not None:
            sql.append("AND CAST(amount AS REAL) <= ?")
            params.append(float(max_amount))
        sql.append("ORDER BY occurred_on, created_at")
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def aggregate_records(self, ledger_id: str, *, start: Optional[date] = None,
                          end: Optional[date] = None) -> list[sqlite3.Row]:
        """按月/分类/成员聚合时用，避免把明细全部取到 Python 里再遍历。"""
        sql = ["SELECT direction, category_id, user_id, SUM(CAST(amount AS REAL)) AS total,"
               " COUNT(*) AS cnt FROM records WHERE ledger_id=?"
               " AND state='approved' AND void_reason IS NULL AND replaced_by IS NULL"]
        params: list[object] = [ledger_id]
        if start:
            sql.append("AND occurred_on >= ?")
            params.append(start.isoformat())
        if end:
            sql.append("AND occurred_on <= ?")
            params.append(end.isoformat())
        sql.append("GROUP BY direction, category_id, user_id")
        return self.conn.execute(" ".join(sql), params).fetchall()

    # ---------------------------------------------------------------- 更正
    def add_correction(self, c: Correction) -> None:
        self.conn.execute(
            "INSERT INTO corrections (correction_id, record_id, proposer_id, reason, old_amount,"
            " new_amount, new_occurred, status, reviewer_id, review_remark, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (c.correction_id, c.record_id, c.proposer_id, c.reason, str(c.old_amount),
             str(c.new_amount), c.new_occurred_on.isoformat(), c.status.value,
             c.reviewer_id, c.review_remark, c.created_at.isoformat()),
        )

    def update_correction(self, c: Correction) -> None:
        self.conn.execute(
            "UPDATE corrections SET status=?, reviewer_id=?, review_remark=? WHERE correction_id=?",
            (c.status.value, c.reviewer_id, c.review_remark, c.correction_id),
        )

    def get_correction(self, correction_id: str) -> Optional[Correction]:
        row = self.conn.execute(
            "SELECT * FROM corrections WHERE correction_id=?", (correction_id,)
        ).fetchone()
        if row is None:
            return None
        return Correction(
            correction_id=row["correction_id"], record_id=row["record_id"],
            proposer_id=row["proposer_id"], reason=row["reason"],
            old_amount=Decimal(row["old_amount"]), new_amount=Decimal(row["new_amount"]),
            new_occurred_on=_d(row["new_occurred"]), status=CorrectionStatus(row["status"]),
            reviewer_id=row["reviewer_id"], review_remark=row["review_remark"],
            created_at=_dt(row["created_at"]),
        )

    def list_pending_corrections(self, record_id: str) -> list[Correction]:
        rows = self.conn.execute(
            "SELECT correction_id FROM corrections WHERE record_id=? AND status='pending'",
            (record_id,),
        ).fetchall()
        return [self.get_correction(r["correction_id"]) for r in rows]  # type: ignore[misc]

    # ---------------------------------------------------------------- 预算
    def upsert_budget(self, b: Budget) -> None:
        self.conn.execute(
            "INSERT INTO budgets (budget_id, ledger_id, period, amount, overrun_notified)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(ledger_id, period) DO UPDATE SET amount=excluded.amount,"
            " overrun_notified=excluded.overrun_notified",
            (b.budget_id, b.ledger_id, b.period, str(b.amount), int(b.overrun_notified)),
        )

    def get_budget(self, ledger_id: str, period: str) -> Optional[Budget]:
        row = self.conn.execute(
            "SELECT * FROM budgets WHERE ledger_id=? AND period=?", (ledger_id, period)
        ).fetchone()
        if row is None:
            return None
        return Budget(row["budget_id"], row["ledger_id"], row["period"],
                      Decimal(row["amount"]), bool(row["overrun_notified"]))

    def mark_budget_notified(self, budget_id: str) -> None:
        self.conn.execute("UPDATE budgets SET overrun_notified=1 WHERE budget_id=?", (budget_id,))

    # ---------------------------------------------------------------- 结算
    def add_settlement(self, s: Settlement) -> None:
        import json

        payload = json.dumps({
            "total_expense": str(s.snapshot.total_expense),
            "total_income": str(s.snapshot.total_income),
            "by_category": {k: str(v) for k, v in s.snapshot.by_category.items()},
            "by_member": {k: str(v) for k, v in s.snapshot.by_member.items()},
            "valid": s.snapshot.valid,
        }, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO settlements (settlement_id, ledger_id, period, payload, locked, version,"
            " operator_id, reopened_by, reopen_reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s.settlement_id, s.ledger_id, s.period, payload, int(s.locked), s.version,
             s.operator_id, s.reopened_by, s.reopen_reason, s.created_at.isoformat()),
        )

    def invalidate_settlements(self, ledger_id: str, period: str) -> None:
        """重开账期时把当期旧快照标记为历史失效。"""
        rows = self.conn.execute(
            "SELECT settlement_id, payload FROM settlements WHERE ledger_id=? AND period=?",
            (ledger_id, period),
        ).fetchall()
        import json

        for row in rows:
            payload = json.loads(row["payload"])
            payload["valid"] = False
            self.conn.execute(
                "UPDATE settlements SET payload=?, locked=0 WHERE settlement_id=?",
                (json.dumps(payload, ensure_ascii=False), row["settlement_id"]),
            )

    def list_settlements(self, ledger_id: str) -> list[Settlement]:
        import json

        rows = self.conn.execute(
            "SELECT * FROM settlements WHERE ledger_id=? ORDER BY period DESC, created_at DESC",
            (ledger_id,),
        ).fetchall()
        result = []
        for r in rows:
            payload = json.loads(r["payload"])
            snap = SettlementSnapshot(
                period=r["period"],
                total_expense=Decimal(payload["total_expense"]),
                total_income=Decimal(payload["total_income"]),
                by_category={k: Decimal(v) for k, v in payload["by_category"].items()},
                by_member={k: Decimal(v) for k, v in payload["by_member"].items()},
                valid=payload["valid"],
            )
            result.append(Settlement(r["settlement_id"], r["ledger_id"], r["period"], snap,
                                     bool(r["locked"]), r["version"], r["operator_id"],
                                     r["reopened_by"], r["reopen_reason"], _dt(r["created_at"])))
        return result

    def current_settlement(self, ledger_id: str, period: str) -> Optional[Settlement]:
        for s in self.list_settlements(ledger_id):
            if s.period == period and s.snapshot.valid:
                return s
        return None

    def is_period_locked(self, ledger_id: str, period: str) -> bool:
        s = self.current_settlement(ledger_id, period)
        return bool(s and s.locked)

    # ---------------------------------------------------------------- 审计
    def add_audit(self, entry: AuditEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit_logs (log_id, ledger_id, actor_id, action, target, result, before,"
            " after, reason, occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry.log_id, entry.ledger_id, entry.actor_id, entry.action, entry.target,
             entry.result, entry.before, entry.after, entry.reason, entry.occurred_at.isoformat()),
        )

    def list_audit(self, ledger_id: str, limit: int = 200) -> list[AuditEntry]:
        rows = self.conn.execute(
            "SELECT * FROM audit_logs WHERE ledger_id=? ORDER BY occurred_at DESC LIMIT ?",
            (ledger_id, limit),
        ).fetchall()
        return [AuditEntry(r["log_id"], r["ledger_id"], r["actor_id"], r["action"], r["target"],
                           r["result"], r["before"], r["after"], r["reason"], _dt(r["occurred_at"]))
                for r in rows]

    # ---------------------------------------------------------------- 通知
    def add_notification(self, n: Notification) -> None:
        self.conn.execute(
            "INSERT INTO notifications (notification_id, ledger_id, user_id, kind, body,"
            " delivered_at, read) VALUES (?,?,?,?,?,?,?)",
            (n.notification_id, n.ledger_id, n.user_id, n.kind, n.body,
             n.delivered_at.isoformat(), int(n.read)),
        )

    def list_notifications(self, user_id: str, unread_only: bool = False) -> list[Notification]:
        sql = "SELECT * FROM notifications WHERE user_id=?"
        if unread_only:
            sql += " AND read=0"
        rows = self.conn.execute(sql + " ORDER BY delivered_at DESC", (user_id,)).fetchall()
        return [Notification(r["notification_id"], r["ledger_id"], r["user_id"], r["kind"],
                             r["body"], _dt(r["delivered_at"]), bool(r["read"])) for r in rows]

    # ---------------------------------------------------------------- 邀请
    def add_invite(self, token: str, ledger_id: str, inviter_id: str, role: RoleType,
                   expires_at: datetime) -> None:
        """邀请写进库，进程重启后仍可用；七天有效期由 expires_at 控制。"""
        self.conn.execute(
            "INSERT INTO invites (token, ledger_id, inviter_id, role, expires_at, used_by, revoked)"
            " VALUES (?,?,?,?,?,NULL,0)",
            (token, ledger_id, inviter_id, role.value, expires_at.isoformat()),
        )

    def get_invite(self, token: str) -> Optional[dict[str, object]]:
        row = self.conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
        if row is None:
            return None
        return {
            "token": row["token"],
            "ledger_id": row["ledger_id"],
            "inviter_id": row["inviter_id"],
            "role": RoleType(row["role"]),
            "expires_at": _dt(row["expires_at"]),
            "used_by": row["used_by"],
            "revoked": bool(row["revoked"]),
        }

    def mark_invite_used(self, token: str, user_id: str) -> None:
        self.conn.execute("UPDATE invites SET used_by=? WHERE token=?", (user_id, token))

    def revoke_invite(self, token: str) -> None:
        self.conn.execute("UPDATE invites SET revoked=1 WHERE token=?", (token,))
