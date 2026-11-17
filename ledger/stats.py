"""查询与统计：组合筛选、分类汇总、成员汇总与预算对比。

统计口径固定：只计入“已审核、未作废、未被新版本替代”的记录。
草稿、待审核、已驳回、已撤回一律不计入，这一点在实验一里反复确认过。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from .models import (
    Direction,
    DomainError,
    RecordState,
    TransactionRecord,
)
from .store import Store

EFFECTIVE = {RecordState.APPROVED}
NON_DEFAULT = {RecordState.DRAFT, RecordState.REJECTED, RecordState.WITHDRAWN,
               RecordState.VOIDED, RecordState.PENDING}


@dataclass
class RecordFilter:
    """多条件查询。字段为空表示不限制。"""

    category_id: Optional[str] = None
    member_id: Optional[str] = None
    start: Optional[date] = None
    end: Optional[date] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    include_non_effective: bool = False
    only_effective: bool = True

    def matches(self, rec: TransactionRecord) -> bool:
        if self.only_effective and not self.include_non_effective:
            if not rec.effective:
                return False
        if self.category_id and rec.category_id != self.category_id:
            return False
        if self.member_id and rec.user_id != self.member_id:
            return False
        if self.start and rec.occurred_on < self.start:
            return False
        if self.end and rec.occurred_on > self.end:
            return False
        if self.min_amount is not None and rec.amount < self.min_amount:
            return False
        if self.max_amount is not None and rec.amount > self.max_amount:
            return False
        return True


@dataclass
class Summary:
    """一段时间内的汇总结果。金额保留两位小数。"""

    expense: Decimal = Decimal("0.00")
    income: Decimal = Decimal("0.00")
    count: int = 0
    by_category: dict[str, Decimal] = field(default_factory=dict)
    by_member: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return self.income - self.expense


class QueryService:
    """只读查询与统计，不做任何写操作。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------ 明细
    def query(self, ledger_id: str, flt: Optional[RecordFilter] = None) -> list[TransactionRecord]:
        """按条件筛明细。默认只看有效记录，勾选后可以查草稿与历史版本。

        条件全部下推给 SQL；如果要求包含非有效记录，才退回到逐行判断。
        """
        if flt is None:
            flt = RecordFilter()
        if flt.include_non_effective:
            rows = self.store.list_records(ledger_id)
            return [r for r in rows if flt.matches(r)]
        return self.store.query_records(
            ledger_id,
            category_id=flt.category_id,
            member_id=flt.member_id,
            start=flt.start,
            end=flt.end,
            min_amount=flt.min_amount,
            max_amount=flt.max_amount,
            effective_only=True,
        )

    def query_one_day(self, ledger_id: str, day: date) -> list[TransactionRecord]:
        return self.query(ledger_id, RecordFilter(start=day, end=day))

    # ------------------------------------------------------------ 汇总
    def monthly_summary(self, ledger_id: str, period: str,
                        include_non_effective: bool = False) -> Summary:
        """按月汇总。period 形如 2026-10。"""
        try:
            year, month = (int(x) for x in period.split("-"))
        except ValueError as exc:
            raise DomainError("PERIOD_INVALID", f"账期格式应为 YYYY-MM：{period}") from exc
        rows = self.store.list_records(ledger_id)
        result = Summary()
        by_cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        by_member: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for rec in rows:
            if rec.occurred_on.year != year or rec.occurred_on.month != month:
                continue
            if not include_non_effective and not rec.effective:
                continue
            if rec.direction == Direction.EXPENSE:
                result.expense += rec.amount
                by_cat[rec.category_id] += rec.amount
                by_member[rec.user_id] += rec.amount
            else:
                result.income += rec.amount
            result.count += 1
        result.by_category = dict(by_cat)
        result.by_member = dict(by_member)
        return result

    def by_category(self, ledger_id: str, period: str) -> list[tuple[str, Decimal, int]]:
        """分类统计：只统计支出，返回 (类别名, 金额, 笔数)，按金额降序。"""
        rows = self.store.list_records(ledger_id)
        bucket: dict[str, list[Decimal]] = defaultdict(list)
        categories = {c.category_id: c for c in self.store.list_categories(ledger_id)}
        for rec in rows:
            if rec.direction != Direction.EXPENSE or not rec.effective:
                continue
            if rec.occurred_on.strftime("%Y-%m") != period:
                continue
            bucket[rec.category_id].append(rec.amount)
        result = []
        for cat_id, amounts in bucket.items():
            name = categories[cat_id].name if cat_id in categories else cat_id
            result.append((name, sum(amounts, Decimal("0.00")), len(amounts)))
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    def by_member(self, ledger_id: str, period: str) -> list[tuple[str, Decimal, int]]:
        """成员统计：按录入人汇总支出，成员退出后仍保留其历史金额。"""
        rows = self.store.list_records(ledger_id)
        bucket: dict[str, list[Decimal]] = defaultdict(list)
        for rec in rows:
            if rec.direction != Direction.EXPENSE or not rec.effective:
                continue
            if rec.occurred_on.strftime("%Y-%m") != period:
                continue
            bucket[rec.user_id].append(rec.amount)
        result = [(uid, sum(v, Decimal("0.00")), len(v)) for uid, v in bucket.items()]
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    # ------------------------------------------------------------ 预算
    def budget_status(self, ledger_id: str, period: str) -> dict[str, object]:
        """预算使用情况。没设预算时返回 has_budget=False，不当成 0。"""
        budget = self.store.get_budget(ledger_id, period)
        summary = self.monthly_summary(ledger_id, period)
        if budget is None:
            return {
                "has_budget": False,
                "amount": None,
                "spent": summary.expense,
                "remaining": None,
                "overrun": False,
            }
        remaining = budget.remaining(summary.expense)
        return {
            "has_budget": True,
            "amount": budget.amount,
            "spent": summary.expense,
            "remaining": remaining,
            "overrun": budget.is_overrun(summary.expense),
        }

    # ------------------------------------------------------------ 审计
    def recent_audit(self, ledger_id: str, limit: int = 50) -> list[dict[str, object]]:
        """审计日志摘要。普通成员只能看自己发起的那部分，由调用方过滤。"""
        entries = self.store.list_audit(ledger_id, limit=limit)
        return [{
            "log_id": e.log_id,
            "actor": e.actor_id,
            "action": e.action,
            "target": e.target,
            "result": e.result,
            "reason": e.reason,
            "at": e.occurred_at.strftime("%Y-%m-%d %H:%M"),
        } for e in entries]

    def audit_for_member(self, ledger_id: str, user_id: str,
                         limit: int = 50) -> list[dict[str, object]]:
        """普通成员只能查自己发起的操作；管理员与所有者可查全量。"""
        from .permissions import Action, ROLE_ACTIONS

        membership = self.store.get_membership(ledger_id, user_id)
        if membership is None:
            raise DomainError("NOT_MEMBER", "不是该账本的当前成员")
        entries = self.recent_audit(ledger_id, limit=limit * 4)
        if Action.VIEW_AUDIT in ROLE_ACTIONS.get(membership.role, set()):
            return entries[:limit]
        return [e for e in entries if e["actor"] == user_id][:limit]
