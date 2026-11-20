"""核心逻辑测试：状态机、权限、统计、结算与金额校验。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ledger import Direction, DomainError, RecordState, RoleType
from ledger.stats import RecordFilter

# ---------------------------------------------------------------- 状态机


def test_draft_only_moves_to_pending(w):
    rec = w.draft()
    assert rec.state == RecordState.DRAFT
    assert rec.can_transition_to(RecordState.PENDING)
    assert not rec.can_transition_to(RecordState.APPROVED)


def test_submit_then_approve(w):
    rec = w.draft()
    result = w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    assert result.record.state == RecordState.PENDING
    approved = w.records.approve(w.ledger.ledger_id, w.admin.user_id, rec.record_id)
    assert approved.state == RecordState.APPROVED


def test_submit_is_idempotent(w):
    rec = w.draft()
    first = w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id, "OP-1")
    second = w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id, "OP-1")
    assert second.repeated is True
    assert second.record.record_id == first.record.record_id


def test_self_review_forbidden(w):
    rec = w.draft(user=w.admin)
    w.records.submit(w.ledger.ledger_id, w.admin.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.records.approve(w.ledger.ledger_id, w.admin.user_id, rec.record_id)
    assert exc.value.code == "SELF_REVIEW_FORBIDDEN"


def test_reject_requires_reason(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.records.reject(w.ledger.ledger_id, w.admin.user_id, rec.record_id, "  ")
    assert exc.value.code == "REASON_REQUIRED"


def test_reject_then_resubmit(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    w.records.reject(w.ledger.ledger_id, w.admin.user_id, rec.record_id, "金额有误")
    again = w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id, "OP-2")
    assert again.record.state == RecordState.PENDING


def test_withdraw_only_pending(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    w.records.withdraw(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError):
        w.records.withdraw(w.ledger.ledger_id, w.member.user_id, rec.record_id)


def test_stale_page_conflict(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    w.records.withdraw(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.records.approve(w.ledger.ledger_id, w.admin.user_id, rec.record_id,
                          expected_state=RecordState.PENDING)
    assert exc.value.code == "STATE_CONFLICT"


def test_personal_ledger_effective_immediately(w):
    personal = w.ledgers.create_ledger(w.owner.user_id, "个人账本", "personal")
    cat = w.ledgers.add_category(personal.ledger_id, w.owner.user_id, "餐饮")
    rec = w.records.create_draft(personal.ledger_id, w.owner.user_id, Direction.EXPENSE,
                                 "18.50", date(2026, 10, 6), cat.category_id)
    result = w.records.submit(personal.ledger_id, w.owner.user_id, rec.record_id)
    assert result.record.state == RecordState.APPROVED


def test_void_removes_from_statistics(w):
    rec = w.approved()
    with pytest.raises(DomainError):
        w.records.void(w.ledger.ledger_id, w.admin.user_id, rec.record_id, "")
    w.records.void(w.ledger.ledger_id, w.admin.user_id, rec.record_id, "记错了")
    assert w.query.monthly_summary(w.ledger.ledger_id, "2026-10").expense == 0


def test_correction_creates_new_version(w):
    rec = w.approved(amount="40.00")
    correction = w.records.propose_correction(
        w.ledger.ledger_id, w.admin.user_id, rec.record_id, "120.00",
        date(2026, 10, 5), "小票看错了")
    new_rec = w.records.approve_correction(w.ledger.ledger_id, w.owner.user_id,
                                           correction.correction_id)
    assert new_rec.version == 2 and new_rec.amount == 120
    assert w.store.get_record(rec.record_id).replaced_by == new_rec.record_id
    assert w.query.monthly_summary(w.ledger.ledger_id, "2026-10").expense == 120


def test_correction_cannot_self_approve(w):
    rec = w.approved()
    correction = w.records.propose_correction(
        w.ledger.ledger_id, w.admin.user_id, rec.record_id, "50.00",
        date(2026, 10, 5), "补记")
    with pytest.raises(DomainError) as exc:
        w.records.approve_correction(w.ledger.ledger_id, w.admin.user_id,
                                     correction.correction_id)
    assert exc.value.code == "SELF_REVIEW_FORBIDDEN"


def test_only_one_pending_correction(w):
    rec = w.approved()
    w.records.propose_correction(w.ledger.ledger_id, w.admin.user_id, rec.record_id,
                                 "50.00", date(2026, 10, 5), "第一次")
    with pytest.raises(DomainError) as exc:
        w.records.propose_correction(w.ledger.ledger_id, w.owner.user_id, rec.record_id,
                                     "60.00", date(2026, 10, 5), "第二次")
    assert exc.value.code == "CORRECTION_PENDING"


def test_txn_no_cannot_reuse_while_pending(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    other = w.draft()
    other.txn_no = rec.txn_no
    w.store.update_record(other)
    with pytest.raises(DomainError) as exc:
        w.records.submit(w.ledger.ledger_id, w.member.user_id, other.record_id)
    assert exc.value.code == "TXN_DUPLICATED"


# ---------------------------------------------------------------- 权限


def test_outsider_has_no_access(w):
    with pytest.raises(DomainError) as exc:
        w.records.create_draft(w.ledger.ledger_id, w.outsider.user_id, "expense",
                               "10.00", date(2026, 10, 5), w.food.category_id)
    assert exc.value.code == "NOT_MEMBER"


def test_member_cannot_review(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError):
        w.records.approve(w.ledger.ledger_id, w.member.user_id, rec.record_id)


def test_only_owner_changes_role(w):
    with pytest.raises(DomainError):
        w.ledgers.change_role(w.ledger.ledger_id, w.admin.user_id, w.member.user_id,
                              RoleType.ADMIN)
    m = w.ledgers.change_role(w.ledger.ledger_id, w.owner.user_id, w.member.user_id,
                              RoleType.ADMIN)
    assert m.role == RoleType.ADMIN


def test_transfer_ownership(w):
    w.ledgers.transfer_ownership(w.ledger.ledger_id, w.owner.user_id, w.admin.user_id)
    assert w.store.get_membership(w.ledger.ledger_id, w.admin.user_id).role == RoleType.OWNER
    assert w.store.get_membership(w.ledger.ledger_id, w.owner.user_id).role == RoleType.ADMIN


def test_owner_cannot_leave_without_transfer(w):
    with pytest.raises(DomainError) as exc:
        w.ledgers.leave(w.ledger.ledger_id, w.owner.user_id)
    assert exc.value.code == "OWNER_MUST_TRANSFER"


def test_member_with_pending_cannot_leave(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.ledgers.leave(w.ledger.ledger_id, w.member.user_id)
    assert exc.value.code == "PENDING_EXISTS"


def test_leave_then_reinvite_reuses_membership(w):
    w.ledgers.leave(w.ledger.ledger_id, w.member.user_id)
    inv = w.ledgers.invite(w.ledger.ledger_id, w.owner.user_id, "member@nju.edu.cn")
    m = w.ledgers.accept_invite(inv.token, w.member.user_id)
    assert m.active


def test_invite_single_use(w):
    inv = w.ledgers.invite(w.ledger.ledger_id, w.owner.user_id, "out@nju.edu.cn")
    w.ledgers.accept_invite(inv.token, w.outsider.user_id)
    with pytest.raises(DomainError):
        w.ledgers.accept_invite(inv.token, w.outsider.user_id)


def test_archived_ledger_read_only(w):
    w.ledgers.archive(w.ledger.ledger_id, w.owner.user_id)
    with pytest.raises(DomainError) as exc:
        w.records.create_draft(w.ledger.ledger_id, w.member.user_id, "expense",
                               "10.00", date(2026, 10, 5), w.food.category_id)
    assert exc.value.code == "LEDGER_ARCHIVED"


def test_audit_scope_for_member(w):
    w.draft()
    mine = w.query.audit_for_member(w.ledger.ledger_id, w.member.user_id)
    assert all(e["actor"] == w.member.user_id for e in mine)
    admin_view = w.query.audit_for_member(w.ledger.ledger_id, w.admin.user_id)
    assert len(admin_view) >= len(mine)


# ---------------------------------------------------------------- 统计


def test_query_filters(w):
    w.approved(amount="30.00", category=w.food)
    w.approved(amount="20.00", category=w.traffic)
    rows = w.query.query(w.ledger.ledger_id, RecordFilter(category_id=w.food.category_id))
    assert len(rows) == 1 and rows[0].amount == Decimal("30.00")


def test_default_query_hides_non_effective(w):
    w.approved(amount="10.00")
    rec = w.draft(amount="99.00")
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    assert all(r.effective for r in w.query.query(w.ledger.ledger_id))


def test_income_not_in_expense(w):
    w.approved(amount="40.00")
    inc = w.draft(amount="200.00", direction="income")
    w.records.submit(w.ledger.ledger_id, w.member.user_id, inc.record_id)
    w.records.approve(w.ledger.ledger_id, w.admin.user_id, inc.record_id)
    s = w.query.monthly_summary(w.ledger.ledger_id, "2026-10")
    assert s.expense == Decimal("40.00") and s.income == Decimal("200.00")


def test_member_summary_keeps_leaver(w):
    w.approved(user=w.member, amount="40.00")
    w.ledgers.leave(w.ledger.ledger_id, w.member.user_id)
    rows = w.query.by_member(w.ledger.ledger_id, "2026-10")
    assert any(a == Decimal("40.00") for _, a, _ in rows)


@pytest.mark.parametrize("raw", ["0", "-1", "1000000.00", "1.234", "abc", ""])
def test_invalid_amount(w, raw):
    with pytest.raises(DomainError):
        w.records.create_draft(w.ledger.ledger_id, w.member.user_id, "expense", raw,
                               date(2026, 10, 5), w.food.category_id)


def test_future_date_rejected(w):
    with pytest.raises(DomainError) as exc:
        w.records.create_draft(w.ledger.ledger_id, w.member.user_id, "expense", "10.00",
                               date.today() + timedelta(days=1), w.food.category_id)
    assert exc.value.code == "DATE_FUTURE"


# ---------------------------------------------------------------- 结算


def test_pending_blocks_settlement(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10",
                        today=date(2026, 11, 4))
    assert exc.value.code == "PENDING_EXISTS"


def test_cannot_settle_current_month(w):
    with pytest.raises(DomainError) as exc:
        w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10",
                        today=date(2026, 10, 20))
    assert exc.value.code == "PERIOD_NOT_ENDED"


def test_settle_then_locked(w):
    w.approved()
    w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10", today=date(2026, 11, 4))
    assert w.settle.is_mutable(w.ledger.ledger_id, "2026-10") is False
    with pytest.raises(DomainError) as exc:
        w.records.create_draft(w.ledger.ledger_id, w.member.user_id, "expense", "10.00",
                               date(2026, 10, 8), w.food.category_id)
    assert exc.value.code == "PERIOD_LOCKED"


def test_reopen_needs_owner_and_reason(w):
    w.approved()
    w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10", today=date(2026, 11, 4))
    with pytest.raises(DomainError):
        w.settle.reopen(w.ledger.ledger_id, w.admin.user_id, "2026-10", "补记")
    s = w.settle.reopen(w.ledger.ledger_id, w.owner.user_id, "2026-10", "漏记房租")
    assert s.locked is False and s.reopen_reason == "漏记房租"


def test_reopen_keeps_history(w):
    w.approved()
    w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10", today=date(2026, 11, 4))
    w.settle.reopen(w.ledger.ledger_id, w.owner.user_id, "2026-10", "补记")
    history = w.settle.history(w.ledger.ledger_id, "2026-10")
    assert len(history) >= 2 and any(not h["valid"] for h in history)


def test_budget_status(w):
    w.budget.set_monthly(w.ledger.ledger_id, w.admin.user_id, "2026-10", "50.00")
    w.approved(amount="40.00")
    status = w.query.budget_status(w.ledger.ledger_id, "2026-10")
    assert status["remaining"] == 10 and status["overrun"] is False
    assert w.query.budget_status(w.ledger.ledger_id, "2026-11")["has_budget"] is False


def test_cross_month_correction(w):
    rec = w.approved(amount="40.00", day=date(2026, 10, 5))
    w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10", today=date(2026, 11, 4))
    w.settle.reopen(w.ledger.ledger_id, w.owner.user_id, "2026-10", "跨月更正")
    correction = w.records.propose_correction(
        w.ledger.ledger_id, w.admin.user_id, rec.record_id, "40.00",
        date(2026, 11, 3), "记错月份")
    w.records.approve_correction(w.ledger.ledger_id, w.owner.user_id, correction.correction_id)
    assert w.query.monthly_summary(w.ledger.ledger_id, "2026-10").expense == 0
    assert w.query.monthly_summary(w.ledger.ledger_id, "2026-11").expense == 40
