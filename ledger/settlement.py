"""预算与结算：按月设置预算、结算锁月、重开账期与快照版本。

结算只处理“已经结束的月份”，而且当期不能有未处理完的记录。
重开必须由所有者填写原因，旧快照保留为历史失效版本。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from . import audit, permissions
from .models import (
    Budget,
    Direction,
    DomainError,
    Settlement,
    SettlementSnapshot,
    RecordState,
    new_id,
)
from .permissions import Action
from .store import Store


def current_period() -> str:
    """以系统日期作为“当前账期”，测试里可替换。"""
    return date.today().strftime("%Y-%m")


def period_of(day: date) -> str:
    return day.strftime("%Y-%m")


def previous_period(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


@dataclass
class SettlementResult:
    settlement: Settlement
    created: bool
    message: str


class BudgetService:
    """预算只做“一个账本、一个自然月、一个总支出额度”。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def set_monthly(self, ledger_id: str, operator_id: str, period: str,
                    amount: object, today: Optional[date] = None) -> Budget:
        from .models import parse_amount

        permissions.check(self.store, ledger_id, operator_id, Action.SET_BUDGET,
                          self.store.get_ledger(ledger_id))
        money = parse_amount(amount)
        if today and period < period_of(today):
            # 允许给已结束的月份补设预算，但不能再改已结算月份
            if self.store.is_period_locked(ledger_id, period):
                raise DomainError("PERIOD_LOCKED", "账期已结算，不能改预算")
        budget = Budget(
            budget_id=new_id("B"),
            ledger_id=ledger_id,
            period=period,
            amount=money,
        )
        with self.store.transaction():
            self.store.upsert_budget(budget)
            audit.record(self.store, ledger_id, operator_id, "set_budget", period,
                         after={"amount": str(money)})
        return budget


class SettlementService:
    """结算与重开。同一账期同时只有一份有效结算。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------ 内部
    def _pending_exists(self, ledger_id: str, period: str) -> bool:
        for rec in self.store.list_records(ledger_id):
            if rec.occurred_on.strftime("%Y-%m") != period:
                continue
            if rec.state == RecordState.PENDING:
                return True
            if self.store.list_pending_corrections(rec.record_id):
                return True
        return False

    def _build_snapshot(self, ledger_id: str, period: str) -> SettlementSnapshot:
        expense = Decimal("0.00")
        income = Decimal("0.00")
        by_cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        by_member: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        categories = {c.category_id: c.name for c in self.store.list_categories(ledger_id)}
        for rec in self.store.list_records(ledger_id):
            if rec.occurred_on.strftime("%Y-%m") != period:
                continue
            if not rec.effective:
                continue
            if rec.direction == Direction.EXPENSE:
                expense += rec.amount
                by_cat[categories.get(rec.category_id, rec.category_id)] += rec.amount
                by_member[rec.user_id] += rec.amount
            else:
                income += rec.amount
        return SettlementSnapshot(
            period=period,
            total_expense=expense,
            total_income=income,
            by_category=dict(by_cat),
            by_member=dict(by_member),
        )

    # ------------------------------------------------------------ 结算
    def settle(self, ledger_id: str, operator_id: str, period: str,
               today: Optional[date] = None, operation_id: Optional[str] = None
               ) -> SettlementResult:
        """执行结算。待审核存在、月份未结束或已结算都会拒绝。"""
        ledger = self.store.get_ledger(ledger_id)
        if ledger is None:
            raise DomainError("LEDGER_NOT_FOUND", "账本不存在")
        permissions.check(self.store, ledger_id, operator_id, Action.SETTLE, ledger)
        ledger.ensure_writable()

        existing = self.store.current_settlement(ledger_id, period)
        if existing is not None:
            return SettlementResult(existing, False, "该账期已结算，返回现有快照")

        now = today or date.today()
        if period >= period_of(now):
            raise DomainError("PERIOD_NOT_ENDED", "只能结算已经结束的月份")
        if self._pending_exists(ledger_id, period):
            raise DomainError("PENDING_EXISTS", "该账期还有待审核记录，不能结算")

        snapshot = self._build_snapshot(ledger_id, period)
        settlement = Settlement(
            settlement_id=new_id("S"),
            ledger_id=ledger_id,
            period=period,
            snapshot=snapshot,
            operator_id=operator_id,
        )
        with self.store.transaction():
            self.store.add_settlement(settlement)
            audit.record(self.store, ledger_id, operator_id, "settle", period,
                         after={"expense": str(snapshot.total_expense),
                                "income": str(snapshot.total_income)})
        audit.notify_members(self.store, ledger_id, "period_settled",
                             f"{period} 已结算并锁定")
        return SettlementResult(settlement, True, "结算完成")

    # ------------------------------------------------------------ 重开
    def reopen(self, ledger_id: str, operator_id: str, period: str, reason: str) -> Settlement:
        """重开账期。只有所有者可以操作，必须写原因。"""
        text = audit.assert_reason(reason)
        permissions.check(self.store, ledger_id, operator_id, Action.REOPEN_PERIOD,
                          self.store.get_ledger(ledger_id))
        current = self.store.current_settlement(ledger_id, period)
        if current is None:
            raise DomainError("NOT_SETTLED", "该账期尚未结算，无需重开")
        with self.store.transaction():
            self.store.invalidate_settlements(ledger_id, period)
            reopened = Settlement(
                settlement_id=new_id("S"),
                ledger_id=ledger_id,
                period=period,
                snapshot=current.snapshot,
                locked=False,
                version=current.version + 1,
                operator_id=operator_id,
                reopened_by=operator_id,
                reopen_reason=text,
            )
            self.store.add_settlement(reopened)
            audit.record(self.store, ledger_id, operator_id, "reopen_period", period,
                         before={"version": current.version}, reason=text)
        audit.notify_members(self.store, ledger_id, "period_reopened",
                             f"{period} 已由所有者重开：{text}")
        return reopened

    # ------------------------------------------------------------ 查询
    def is_mutable(self, ledger_id: str, period: str) -> bool:
        """账期是否还允许写入。已结算锁定返回 False。"""
        return not self.store.is_period_locked(ledger_id, period)

    def history(self, ledger_id: str, period: str) -> list[dict[str, object]]:
        """某账期的结算版本历史，包含已失效的旧快照。"""
        result = []
        for s in self.store.list_settlements(ledger_id):
            if s.period != period:
                continue
            result.append({
                "settlement_id": s.settlement_id,
                "version": s.version,
                "valid": s.snapshot.valid,
                "locked": s.locked,
                "expense": s.snapshot.total_expense,
                "income": s.snapshot.total_income,
                "reason": s.reopen_reason,
                "operator": s.operator_id,
            })
        return result
