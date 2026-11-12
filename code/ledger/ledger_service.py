"""账本与成员服务：建账本、邀请、角色变更、所有权移交与退出。

邀请使用一次性令牌，默认七天过期；已加入的人重复点击不会产生重复成员关系。
所有者退出前必须先把所有权交给一名现有成员，账本不能没有老板。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import audit, permissions
from .models import (
    Category,
    DomainError,
    Ledger,
    LedgerType,
    Membership,
    RoleType,
    User,
    new_id,
    now,
)
from .permissions import Action
from .store import Store

INVITE_TTL_DAYS = 7


@dataclass
class Invite:
    """邀请令牌。过期或已被撤销的令牌不能使用。"""

    token: str
    ledger_id: str
    inviter_id: str
    role: RoleType
    expires_at: datetime
    used_by: Optional[str] = None
    revoked: bool = False

    @property
    def usable(self) -> bool:
        return (not self.revoked) and self.used_by is None and datetime.now() <= self.expires_at


class LedgerService:
    """账本生命周期与成员关系。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._invites: dict[str, Invite] = {}

    # ------------------------------------------------------------ 用户
    def register(self, email: str, nickname: str) -> User:
        if self.store.find_user_by_email(email) is not None:
            raise DomainError("EMAIL_TAKEN", "邮箱已被注册")
        user = User.create(email, nickname)
        with self.store.transaction():
            self.store.add_user(user)
        return user

    def login(self, email: str) -> User:
        """原型阶段用邮箱查用户代替密码校验，密码留作后续 TODO。"""
        user = self.store.find_user_by_email(email)
        if user is None:
            raise DomainError("USER_NOT_FOUND", "账号不存在")
        return user

    # ------------------------------------------------------------ 账本
    def create_ledger(self, owner_id: str, name: str,
                      ledger_type: LedgerType | str) -> Ledger:
        """建账本同时把创建者写成所有者，类型创建后不可改。"""
        if not name.strip():
            raise DomainError("NAME_REQUIRED", "账本名称不能为空")
        if not isinstance(ledger_type, LedgerType):
            ledger_type = LedgerType(ledger_type)
        if self.store.get_user(owner_id) is None:
            raise DomainError("USER_NOT_FOUND", "用户不存在")
        ledger = Ledger(ledger_id=new_id("LG"), name=name.strip(), type=ledger_type)
        membership = Membership(
            membership_id=new_id("M"),
            ledger_id=ledger.ledger_id,
            user_id=owner_id,
            role=RoleType.OWNER,
        )
        with self.store.transaction():
            self.store.add_ledger(ledger)
            self.store.add_membership(membership)
            audit.record(self.store, ledger.ledger_id, owner_id, "create_ledger",
                         ledger.ledger_id, after={"name": ledger.name, "type": ledger_type.value})
        # 默认给一个“其他”类别，避免新账本没有任何可选类别
        self.store.add_category(Category(new_id("CAT"), ledger.ledger_id, "其他"))
        return ledger

    def archive(self, ledger_id: str, operator_id: str) -> Ledger:
        """归档后只读：不能记、不能审、不能结算，成员仍可查看历史。"""
        ledger = self.store.get_ledger(ledger_id)
        if ledger is None:
            raise DomainError("LEDGER_NOT_FOUND", "账本不存在")
        permissions.check(self.store, ledger_id, operator_id, Action.ARCHIVE_LEDGER, ledger)
        with self.store.transaction():
            ledger.archived = True
            self.store.update_ledger(ledger)
            audit.record(self.store, ledger_id, operator_id, "archive_ledger", ledger_id)
        return ledger

    def restore(self, ledger_id: str, operator_id: str) -> Ledger:
        ledger = self.store.get_ledger(ledger_id)
        if ledger is None:
            raise DomainError("LEDGER_NOT_FOUND", "账本不存在")
        membership = permissions.require_member(self.store, ledger_id, operator_id)
        if membership.role != RoleType.OWNER:
            raise DomainError("PERMISSION_DENIED", "只有所有者可以恢复归档账本")
        with self.store.transaction():
            ledger.archived = False
            self.store.update_ledger(ledger)
            audit.record(self.store, ledger_id, operator_id, "restore_ledger", ledger_id)
        return ledger

    # ------------------------------------------------------------ 邀请
    def invite(self, ledger_id: str, inviter_id: str, email: str,
               role: RoleType = RoleType.MEMBER) -> Invite:
        permissions.check(self.store, ledger_id, inviter_id, Action.INVITE,
                          self.store.get_ledger(ledger_id))
        target = self.store.find_user_by_email(email)
        if target is None:
            raise DomainError("USER_NOT_FOUND", "被邀请人还没有注册")
        existing = self.store.get_membership(ledger_id, target.user_id)
        if existing is not None:
            raise DomainError("ALREADY_MEMBER", "该用户已在账本中")
        if role == RoleType.OWNER:
            raise DomainError("INVITE_ROLE_INVALID", "不能通过邀请直接授予所有者")
        if not isinstance(role, RoleType):
            role = RoleType(role)
        token = secrets.token_urlsafe(12)
        expires_at = now() + timedelta(days=INVITE_TTL_DAYS)
        with self.store.transaction():
            self.store.add_invite(token, ledger_id, inviter_id, role, expires_at)
            audit.record(self.store, ledger_id, inviter_id, "invite", target.user_id,
                         after={"role": role.value})
        invite = Invite(token=token, ledger_id=ledger_id, inviter_id=inviter_id,
                        role=role, expires_at=expires_at)
        audit.notify(self.store, ledger_id, target.user_id, "invite",
                     f"你被邀请加入账本 {ledger_id}，七天内在应用内确认")
        return invite

    def accept_invite(self, token: str, user_id: str) -> Membership:
        row = self.store.get_invite(token)
        if row is None:
            raise DomainError("INVITE_NOT_FOUND", "邀请不存在")
        expired = datetime.now() > row["expires_at"]  # type: ignore[operator]
        if row["revoked"] or row["used_by"] is not None or expired:
            raise DomainError("INVITE_EXPIRED", "邀请已过期或被撤销")
        ledger_id = str(row["ledger_id"])
        role = row["role"]
        if self.store.get_membership(ledger_id, user_id) is not None:
            raise DomainError("ALREADY_MEMBER", "已经在账本中，不重复加入")

        # 退出过的成员再被邀请时复用原来的成员关系，历史记录不另起一条
        existing = self.store.get_membership_any(ledger_id, user_id)
        if existing is not None:
            with self.store.transaction():
                existing.left_at = None
                existing.role = role  # type: ignore[assignment]
                existing.joined_at = now()
                self.store.update_membership(existing)
                self.store.mark_invite_used(token, user_id)
                audit.record(self.store, ledger_id, user_id, "rejoin",
                             existing.membership_id, after={"role": existing.role.value})
            audit.notify_members(self.store, ledger_id, "member_joined",
                                 f"成员回来了：{user_id}", exclude=user_id)
            return existing

        membership = Membership(
            membership_id=new_id("M"),
            ledger_id=ledger_id,
            user_id=user_id,
            role=role,  # type: ignore[arg-type]
        )
        with self.store.transaction():
            self.store.add_membership(membership)
            self.store.mark_invite_used(token, user_id)
            audit.record(self.store, ledger_id, user_id, "accept_invite",
                         membership.membership_id, after={"role": membership.role.value})
        audit.notify_members(self.store, ledger_id, "member_joined",
                             f"新成员加入：{user_id}", exclude=user_id)
        return membership

    def revoke_invite(self, token: str, operator_id: str) -> None:
        row = self.store.get_invite(token)
        if row is None:
            raise DomainError("INVITE_NOT_FOUND", "邀请不存在")
        ledger_id = str(row["ledger_id"])
        permissions.check(self.store, ledger_id, operator_id, Action.INVITE,
                          self.store.get_ledger(ledger_id))
        with self.store.transaction():
            self.store.revoke_invite(token)
            audit.record(self.store, ledger_id, operator_id, "revoke_invite", token)

    # ------------------------------------------------------------ 角色
    def change_role(self, ledger_id: str, operator_id: str, target_id: str,
                    role: RoleType) -> Membership:
        """改角色。只有所有者能任免管理员；不能把别人直接提成所有者。"""
        permissions.check(self.store, ledger_id, operator_id, Action.INVITE,
                          self.store.get_ledger(ledger_id))
        operator = permissions.require_member(self.store, ledger_id, operator_id)
        if operator.role != RoleType.OWNER:
            raise DomainError("PERMISSION_DENIED", "只有所有者可以调整角色")
        if role == RoleType.OWNER:
            raise DomainError("ROLE_INVALID", "所有者只能通过移交产生")
        target = self.store.get_membership(ledger_id, target_id)
        if target is None:
            raise DomainError("NOT_MEMBER", "目标用户不是当前成员")
        with self.store.transaction():
            before = target.role
            target.role = role
            self.store.update_membership(target)
            audit.record(self.store, ledger_id, operator_id, "change_role", target_id,
                         before={"role": before.value}, after={"role": role.value})
        audit.notify(self.store, ledger_id, target_id, "role_changed",
                     f"你在账本 {ledger_id} 的角色已调整为 {role.value}")
        return target

    def transfer_ownership(self, ledger_id: str, owner_id: str, target_id: str) -> None:
        """移交所有权：原所有者降为管理员，账本始终有人负责。"""
        owner = permissions.require_member(self.store, ledger_id, owner_id)
        if owner.role != RoleType.OWNER:
            raise DomainError("PERMISSION_DENIED", "只有所有者可以移交")
        target = self.store.get_membership(ledger_id, target_id)
        if target is None:
            raise DomainError("NOT_MEMBER", "目标用户不是当前成员")
        if target_id == owner_id:
            raise DomainError("ROLE_INVALID", "不能移交给自己")
        with self.store.transaction():
            target.role = RoleType.OWNER
            owner.role = RoleType.ADMIN
            self.store.update_membership(target)
            self.store.update_membership(owner)
            audit.record(self.store, ledger_id, owner_id, "transfer_ownership", target_id)
        audit.notify(self.store, ledger_id, target_id, "ownership_transferred",
                     "你已成为账本所有者")

    def leave(self, ledger_id: str, user_id: str) -> None:
        """退出账本。所有者必须先移交；退出后不再有访问权限。"""
        membership = self.store.get_membership(ledger_id, user_id)
        if membership is None:
            raise DomainError("NOT_MEMBER", "不是该账本的当前成员")
        if membership.role == RoleType.OWNER:
            raise DomainError("OWNER_MUST_TRANSFER", "所有者退出前必须先移交所有权")
        pending = [r for r in self.store.list_records(ledger_id, states=None)
                   if r.user_id == user_id and r.state.value == "pending"]
        if pending:
            raise DomainError("PENDING_EXISTS", "还有待审核记录，请先撤回或等待审核")
        with self.store.transaction():
            membership.left_at = now()
            self.store.update_membership(membership)
            audit.record(self.store, ledger_id, user_id, "leave_ledger", membership.membership_id)
        audit.notify_members(self.store, ledger_id, "member_left",
                             f"成员退出：{user_id}", exclude=user_id)

    # ------------------------------------------------------------ 类别
    def add_category(self, ledger_id: str, operator_id: str, name: str) -> Category:
        permissions.require_member(self.store, ledger_id, operator_id)
        if self.store.find_category(ledger_id, name) is not None:
            raise DomainError("CATEGORY_EXISTS", "类别已存在")
        category = Category(new_id("CAT"), ledger_id, name.strip())
        with self.store.transaction():
            self.store.add_category(category)
            audit.record(self.store, ledger_id, operator_id, "add_category", name)
        return category

    def disable_category(self, ledger_id: str, operator_id: str, category_id: str) -> None:
        """停用类别。停用不影响历史统计，只禁止新记录选用。"""
        permissions.check(self.store, ledger_id, operator_id, Action.SET_BUDGET,
                          self.store.get_ledger(ledger_id))
        category = self.store.get_category(category_id)
        if category is None or category.ledger_id != ledger_id:
            raise DomainError("CATEGORY_NOT_FOUND", "类别不存在")
        with self.store.transaction():
            self.store.disable_category(category_id)
            audit.record(self.store, ledger_id, operator_id, "disable_category", category_id)
