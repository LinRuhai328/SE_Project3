"""记录服务：新增、提交、审核、驳回、撤回、作废与更正。

这一层是写路径的唯一入口。权限、状态机、幂等、交易号占用与账期锁定都在
这里判定，界面层不做任何业务判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import audit, permissions
from .models import (
    Correction,
    CorrectionStatus,
    Direction,
    DomainError,
    Ledger,
    LedgerType,
    RecordState,
    TransactionRecord,
    new_id,
    parse_amount,
    parse_occurred_on,
)
from .permissions import Action
from .store import Store


@dataclass
class SubmitResult:
    """提交结果。重复提交时 repeated=True，返回首次处理的那条记录。"""

    record: TransactionRecord
    repeated: bool
    message: str


class RecordService:
    """收支记录的全部写操作。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------ 内部工具
    def _ledger(self, ledger_id: str) -> Ledger:
        ledger = self.store.get_ledger(ledger_id)
        if ledger is None:
            raise DomainError("LEDGER_NOT_FOUND", "账本不存在")
        return ledger

    def _record(self, record_id: str) -> TransactionRecord:
        rec = self.store.get_record(record_id)
        if rec is None:
            raise DomainError("RECORD_NOT_FOUND", "记录不存在")
        return rec

    def _assert_period_open(self, ledger_id: str, occurred_on: date) -> None:
        """已结算并锁定的月份拒绝新增、更正与作废。"""
        period = occurred_on.strftime("%Y-%m")
        if self.store.is_period_locked(ledger_id, period):
            raise DomainError("PERIOD_LOCKED", f"{period} 已结算并锁定，需由所有者重开")

    def _assert_txn_available(self, ledger_id: str, txn_no: str) -> None:
        """同一交易号在“待审核或已审核未作废”中最多一条。"""
        for other in self.store.find_by_txn_no(ledger_id, txn_no):
            occupied = {RecordState.PENDING, RecordState.APPROVED}
            if other.state in occupied and other.void_reason is None:
                raise DomainError("TXN_DUPLICATED", f"交易号 {txn_no} 已被占用")

    # ------------------------------------------------------------ 新增
    def create_draft(self, ledger_id: str, user_id: str, direction: Direction | str,
                     amount: object, occurred_on: object, category_id: str,
                     remark: str = "", txn_no: Optional[str] = None) -> TransactionRecord:
        """创建草稿。这一步不校验账期锁定，因为草稿不进入统计。"""
        ledger = self._ledger(ledger_id)
        permissions.check(self.store, ledger_id, user_id, Action.CREATE_RECORD, ledger)
        ledger.ensure_writable()

        if not isinstance(direction, Direction):
            direction = Direction(direction)
        money = parse_amount(amount)
        occurred = parse_occurred_on(occurred_on)
        # 已结算并锁定的月份不再接受新记录，草稿也不例外：
        # 否则用户会先存一堆草稿，再发现整月都提交不了。
        self._assert_period_open(ledger_id, occurred)
        category = self.store.get_category(category_id)
        if category is None or category.ledger_id != ledger_id:
            raise DomainError("CATEGORY_NOT_FOUND", "类别不存在或不属于该账本")
        if category.disabled:
            raise DomainError("CATEGORY_DISABLED", "类别已停用，不能用于新记录")

        rec = TransactionRecord(
            record_id=new_id("R"),
            ledger_id=ledger_id,
            user_id=user_id,
            direction=direction,
            amount=money,
            occurred_on=occurred,
            category_id=category_id,
            state=RecordState.DRAFT,
            txn_no=txn_no or new_id("TX"),
            remark=remark.strip(),
        )
        with self.store.transaction():
            self.store.add_record(rec)
            audit.record(self.store, ledger_id, user_id, "create_draft", rec.record_id,
                         after={"amount": str(rec.amount), "state": rec.state.value})
        return rec

    # ------------------------------------------------------------ 提交
    def submit(self, ledger_id: str, user_id: str, record_id: str,
               operation_id: Optional[str] = None) -> SubmitResult:
        """提交草稿进入待审核，或把驳回/撤回的记录重新提交。

        幂等：同一 operation_id 重复调用返回首次结果，不产生第二条记录。
        """
        op_id = operation_id or new_id("OP")
        existing = self.store.find_by_operation(ledger_id, op_id)
        if existing is not None:
            return SubmitResult(existing, True, "重复提交，返回首次处理结果")

        rec = self._record(record_id)
        if rec.ledger_id != ledger_id:
            raise DomainError("RECORD_NOT_FOUND", "记录不属于该账本")
        permissions.check(self.store, ledger_id, user_id, Action.SUBMIT_RECORD,
                          self._ledger(ledger_id))
        if rec.user_id != user_id:
            raise DomainError("NOT_SUBMITTER", "只能提交自己录入的记录")
        if rec.state not in {RecordState.DRAFT, RecordState.REJECTED, RecordState.WITHDRAWN}:
            raise DomainError("STATE_ILLEGAL", f"当前状态 {rec.state.value} 不能提交")

        self._assert_period_open(ledger_id, rec.occurred_on)
        if rec.txn_no:
            self._assert_txn_available(ledger_id, rec.txn_no)

        ledger = self._ledger(ledger_id)
        with self.store.transaction():
            rec.transition(RecordState.PENDING)
            rec.operation_id = op_id
            rec.reject_reason = None
            # 个人账本本人确认即生效，共享账本必须等他人审核
            if ledger.type == LedgerType.PERSONAL:
                rec.transition(RecordState.APPROVED)
            self.store.update_record(rec)
            audit.record(self.store, ledger_id, user_id, "submit", rec.record_id,
                         after={"state": rec.state.value, "operation_id": op_id})
        if ledger.type == LedgerType.PERSONAL:
            audit.notify(self.store, ledger_id, user_id, "record_effective",
                         f"记录 {rec.record_id} 已生效")
        else:
            audit.notify_members(self.store, ledger_id, "record_pending",
                                 f"有新的待审核记录 {rec.record_id}", exclude=None)
        return SubmitResult(rec, False, "已提交")

    # ------------------------------------------------------------ 审核
    def approve(self, ledger_id: str, reviewer_id: str, record_id: str,
                expected_state: Optional[RecordState] = None) -> TransactionRecord:
        """审核通过。expected_state 用于防止旧页面覆盖新状态。"""
        rec = self._record(record_id)
        if rec.ledger_id != ledger_id:
            raise DomainError("RECORD_NOT_FOUND", "记录不属于该账本")
        ledger = self._ledger(ledger_id)
        permissions.check(self.store, ledger_id, reviewer_id, Action.REVIEW_RECORD, ledger)
        if not permissions.can_review(self.store, ledger_id, reviewer_id, rec.user_id):
            raise DomainError("SELF_REVIEW_FORBIDDEN", "不能审核自己提交的记录")
        if expected_state is not None and rec.state != expected_state:
            raise DomainError("STATE_CONFLICT",
                              f"记录状态已变为 {rec.state.value}，请刷新后再操作")
        if rec.state != RecordState.PENDING:
            raise DomainError("STATE_ILLEGAL", "只有待审核的记录可以审核")
        self._assert_period_open(ledger_id, rec.occurred_on)

        with self.store.transaction():
            before = {"state": rec.state.value}
            rec.transition(RecordState.APPROVED)
            self.store.update_record(rec)
            audit.record(self.store, ledger_id, reviewer_id, "approve", rec.record_id,
                         before=before, after={"state": rec.state.value})
        audit.notify(self.store, ledger_id, rec.user_id, "review_result",
                     f"记录 {rec.record_id} 审核通过")
        audit.notify_members(self.store, ledger_id, "record_effective",
                             f"记录 {rec.record_id} 已生效", exclude=rec.user_id)
        return rec

    def reject(self, ledger_id: str, reviewer_id: str, record_id: str, reason: str,
               expected_state: Optional[RecordState] = None) -> TransactionRecord:
        """驳回，必须写明原因。原因会进入审计并通知提交人。"""
        text = audit.assert_reason(reason)
        rec = self._record(record_id)
        if rec.ledger_id != ledger_id:
            raise DomainError("RECORD_NOT_FOUND", "记录不属于该账本")
        permissions.check(self.store, ledger_id, reviewer_id, Action.REVIEW_RECORD,
                          self._ledger(ledger_id))
        if not permissions.can_review(self.store, ledger_id, reviewer_id, rec.user_id):
            raise DomainError("SELF_REVIEW_FORBIDDEN", "不能驳回自己提交的记录")
        if expected_state is not None and rec.state != expected_state:
            raise DomainError("STATE_CONFLICT", "记录状态已变化，请刷新后再操作")
        if rec.state != RecordState.PENDING:
            raise DomainError("STATE_ILLEGAL", "只有待审核的记录可以驳回")

        with self.store.transaction():
            rec.transition(RecordState.REJECTED)
            rec.reject_reason = text
            self.store.update_record(rec)
            audit.record(self.store, ledger_id, reviewer_id, "reject", rec.record_id,
                         after={"state": rec.state.value}, reason=text)
        audit.notify(self.store, ledger_id, rec.user_id, "review_result",
                     f"记录 {rec.record_id} 被驳回：{text}")
        return rec

    # ------------------------------------------------------------ 撤回
    def withdraw(self, ledger_id: str, user_id: str, record_id: str) -> TransactionRecord:
        """提交人撤回待审核记录。已审核的不能撤回，只能走更正或作废。"""
        rec = self._record(record_id)
        if rec.user_id != user_id:
            raise DomainError("NOT_SUBMITTER", "只能撤回自己提交的记录")
        if rec.state != RecordState.PENDING:
            raise DomainError("STATE_ILLEGAL", "只有待审核的记录可以撤回")
        with self.store.transaction():
            rec.transition(RecordState.WITHDRAWN)
            self.store.update_record(rec)
            audit.record(self.store, ledger_id, user_id, "withdraw", rec.record_id,
                         after={"state": rec.state.value})
        return rec

    # ------------------------------------------------------------ 作废
    def void(self, ledger_id: str, operator_id: str, record_id: str,
             reason: str) -> TransactionRecord:
        """作废已审核记录。作废后不计入统计，记录本身保留。"""
        text = audit.assert_reason(reason)
        rec = self._record(record_id)
        permissions.check(self.store, ledger_id, operator_id, Action.VOID_RECORD,
                          self._ledger(ledger_id))
        if rec.state != RecordState.APPROVED:
            raise DomainError("STATE_ILLEGAL", "只有已审核的记录可以作废")
        if rec.replaced_by is not None:
            raise DomainError("ALREADY_REPLACED", "该记录已被更正版本替代")
        self._assert_period_open(ledger_id, rec.occurred_on)

        with self.store.transaction():
            rec.transition(RecordState.VOIDED)
            rec.void_reason = text
            self.store.update_record(rec)
            audit.record(self.store, ledger_id, operator_id, "void", rec.record_id,
                         before={"state": RecordState.APPROVED.value},
                         after={"state": rec.state.value}, reason=text)
        audit.notify_members(self.store, ledger_id, "record_voided",
                             f"记录 {rec.record_id} 已作废")
        return rec

    # ------------------------------------------------------------ 更正
    def propose_correction(self, ledger_id: str, user_id: str, record_id: str,
                           new_amount: object, new_occurred_on: object,
                           reason: str) -> Correction:
        """对已审核记录发起更正。必须写原因，且要另一个人审核。"""
        text = audit.assert_reason(reason)
        rec = self._record(record_id)
        permissions.check(self.store, ledger_id, user_id, Action.PROPOSE_CORRECTION,
                          self._ledger(ledger_id))
        if rec.state != RecordState.APPROVED or rec.replaced_by is not None:
            raise DomainError("STATE_ILLEGAL", "只有当前有效的已审核记录可以更正")
        self._assert_period_open(ledger_id, rec.occurred_on)
        money = parse_amount(new_amount)
        occurred = parse_occurred_on(new_occurred_on)
        if self.store.list_pending_corrections(record_id):
            raise DomainError("CORRECTION_PENDING", "该记录已有待审核的更正")

        correction = Correction(
            correction_id=new_id("C"),
            record_id=record_id,
            proposer_id=user_id,
            reason=text,
            old_amount=rec.amount,
            new_amount=money,
            new_occurred_on=occurred,
        )
        with self.store.transaction():
            self.store.add_correction(correction)
            audit.record(self.store, ledger_id, user_id, "propose_correction", record_id,
                         after={"old": str(rec.amount), "new": str(money)}, reason=text)
        return correction

    def approve_correction(self, ledger_id: str, reviewer_id: str,
                           correction_id: str) -> TransactionRecord:
        """更正审核通过：生成新版本记录，旧版本标记为被替代。"""
        correction = self.store.get_correction(correction_id)
        if correction is None:
            raise DomainError("CORRECTION_NOT_FOUND", "更正单不存在")
        if correction.status != CorrectionStatus.PENDING:
            raise DomainError("STATE_ILLEGAL", "更正单已处理")
        if correction.proposer_id == reviewer_id:
            raise DomainError("SELF_REVIEW_FORBIDDEN", "不能审核自己发起的更正")
        permissions.check(self.store, ledger_id, reviewer_id, Action.REVIEW_RECORD,
                          self._ledger(ledger_id))

        old = self._record(correction.record_id)
        if old.ledger_id != ledger_id:
            raise DomainError("RECORD_NOT_FOUND", "记录不属于该账本")
        self._assert_period_open(ledger_id, old.occurred_on)

        new_rec = TransactionRecord(
            record_id=new_id("R"),
            ledger_id=ledger_id,
            user_id=old.user_id,
            direction=old.direction,
            amount=correction.new_amount,
            occurred_on=correction.new_occurred_on,
            category_id=old.category_id,
            state=RecordState.APPROVED,
            txn_no=old.txn_no,
            operation_id=None,      # 新版本是新记录，不复用原幂等键
            remark=old.remark,
            version=old.version + 1,
        )
        with self.store.transaction():
            correction.status = CorrectionStatus.APPROVED
            correction.reviewer_id = reviewer_id
            self.store.update_correction(correction)
            old.replaced_by = new_rec.record_id
            self.store.update_record(old)
            self.store.add_record(new_rec)
            audit.record(self.store, ledger_id, reviewer_id, "approve_correction",
                         old.record_id,
                         before={"amount": str(old.amount)},
                         after={"amount": str(new_rec.amount), "new_id": new_rec.record_id})
        audit.notify(self.store, ledger_id, correction.proposer_id, "correction_result",
                     f"更正 {correction_id} 已通过")
        return new_rec

    def reject_correction(self, ledger_id: str, reviewer_id: str, correction_id: str,
                          reason: str) -> Correction:
        text = audit.assert_reason(reason)
        correction = self.store.get_correction(correction_id)
        if correction is None:
            raise DomainError("CORRECTION_NOT_FOUND", "更正单不存在")
        if correction.status != CorrectionStatus.PENDING:
            raise DomainError("STATE_ILLEGAL", "更正单已处理")
        if correction.proposer_id == reviewer_id:
            raise DomainError("SELF_REVIEW_FORBIDDEN", "不能驳回自己发起的更正")
        permissions.check(self.store, ledger_id, reviewer_id, Action.REVIEW_RECORD,
                          self._ledger(ledger_id))
        with self.store.transaction():
            correction.status = CorrectionStatus.REJECTED
            correction.reviewer_id = reviewer_id
            correction.review_remark = text
            self.store.update_correction(correction)
            audit.record(self.store, ledger_id, reviewer_id, "reject_correction",
                         correction.record_id, reason=text)
        return correction

    # ------------------------------------------------------------ 查询
    def list_by_state(self, ledger_id: str, state: RecordState) -> list[TransactionRecord]:
        return self.store.list_records(ledger_id, [state])

    def list_pending(self, ledger_id: str, reviewer_id: str) -> list[TransactionRecord]:
        """待审核列表。按“禁止自审”过滤掉自己提交的记录。"""
        return [r for r in self.list_by_state(ledger_id, RecordState.PENDING)
                if r.user_id != reviewer_id]

    def history_of(self, record_id: str) -> list[TransactionRecord]:
        """一条记录的版本链：从初版到当前生效版本。"""
        chain = []
        current = self.store.get_record(record_id)
        while current is not None:
            chain.append(current)
            current = self.store.get_record(current.replaced_by) if current.replaced_by else None
        return chain
