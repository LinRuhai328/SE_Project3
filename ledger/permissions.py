"""权限判定：把“谁能做什么”集中在一处。

界面层只负责隐藏按钮，真正的拒绝发生在这里。所有写操作先过 check()，
权限不足直接抛 PermissionDenied，避免出现“页面能点、服务端静默失败”。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .models import DomainError, Ledger, Membership, RoleType
from .store import Store


class Action(str, Enum):
    """需要判权的动作。与用例图里的用例一一对应。"""

    CREATE_LEDGER = "create_ledger"
    ARCHIVE_LEDGER = "archive_ledger"
    INVITE = "invite"
    REMOVE_MEMBER = "remove_member"
    TRANSFER_OWNER = "transfer_owner"
    CREATE_RECORD = "create_record"
    SUBMIT_RECORD = "submit_record"
    REVIEW_RECORD = "review_record"
    VOID_RECORD = "void_record"
    PROPOSE_CORRECTION = "propose_correction"
    SET_BUDGET = "set_budget"
    SETTLE = "settle"
    REOPEN_PERIOD = "reopen_period"
    VIEW_AUDIT = "view_audit"


# 角色到允许动作的映射。所有者拥有管理员全部权限，另外多出移交与重开。
ROLE_ACTIONS: dict[RoleType, set[Action]] = {
    RoleType.OWNER: {
        Action.CREATE_LEDGER, Action.ARCHIVE_LEDGER, Action.INVITE, Action.REMOVE_MEMBER,
        Action.TRANSFER_OWNER, Action.CREATE_RECORD, Action.SUBMIT_RECORD, Action.REVIEW_RECORD,
        Action.VOID_RECORD, Action.PROPOSE_CORRECTION, Action.SET_BUDGET, Action.SETTLE,
        Action.REOPEN_PERIOD, Action.VIEW_AUDIT,
    },
    RoleType.ADMIN: {
        Action.INVITE, Action.REMOVE_MEMBER, Action.CREATE_RECORD, Action.SUBMIT_RECORD,
        Action.REVIEW_RECORD, Action.VOID_RECORD, Action.PROPOSE_CORRECTION, Action.SET_BUDGET,
        Action.SETTLE, Action.VIEW_AUDIT,
    },
    RoleType.MEMBER: {
        Action.CREATE_RECORD, Action.SUBMIT_RECORD, Action.PROPOSE_CORRECTION,
    },
}


def require_member(store: Store, ledger_id: str, user_id: str) -> Membership:
    """取当前有效成员关系；非成员或已退出直接拒绝。"""
    membership = store.get_membership(ledger_id, user_id)
    if membership is None:
        raise DomainError("NOT_MEMBER", "不是该账本的当前成员")
    return membership


def check(store: Store, ledger_id: str, user_id: str, action: Action,
          ledger: Optional[Ledger] = None) -> Membership:
    """判权入口。返回成员关系，便于调用方继续判断“是否本人提交”这类条件。"""
    membership = require_member(store, ledger_id, user_id)
    allowed = ROLE_ACTIONS.get(membership.role, set())
    if action not in allowed:
        raise DomainError("PERMISSION_DENIED",
                          f"角色 {membership.role.value} 无权执行 {action.value}")
    if ledger is not None and ledger.archived and action not in {
        Action.VIEW_AUDIT, Action.TRANSFER_OWNER, Action.ARCHIVE_LEDGER
    }:
        raise DomainError("LEDGER_ARCHIVED", "账本已归档，只能查看历史")
    return membership


def can_review(store: Store, ledger_id: str, reviewer_id: str, submitter_id: str) -> bool:
    """审核前置条件：有审核权且不是提交人本人。"""
    membership = store.get_membership(ledger_id, reviewer_id)
    if membership is None:
        return False
    if Action.REVIEW_RECORD not in ROLE_ACTIONS.get(membership.role, set()):
        return False
    return reviewer_id != submitter_id


def is_owner(store: Store, ledger_id: str, user_id: str) -> bool:
    membership = store.get_membership(ledger_id, user_id)
    return bool(membership and membership.role == RoleType.OWNER)
