"""领域模型：用户、账本、成员关系、记录、预算、结算与审计。

本模块只放数据结构和状态枚举，不依赖数据库，便于单元测试单独构造对象。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional

# 金额上限与精度，取自需求 REQ-005
MAX_AMOUNT = Decimal("999999.99")
MIN_AMOUNT = Decimal("0.01")
CENT = Decimal("0.01")


class DomainError(Exception):
    """业务规则被违反时抛出，携带一个稳定的错误码便于前端判断。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.code}] {self.message}"


class LedgerType(str, Enum):
    """账本类型。创建后不可修改，需要换类型只能新建一个账本。"""

    PERSONAL = "personal"
    SHARED = "shared"


class RoleType(str, Enum):
    """账本内的三类角色，权限差异见 permissions 模块。"""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Direction(str, Enum):
    """收支方向。用类型区分正负，金额本身恒为正数。"""

    INCOME = "income"
    EXPENSE = "expense"


class RecordState(str, Enum):
    """收支记录的六种状态。任何状态都不得退回草稿。"""

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    VOIDED = "voided"


class CorrectionStatus(str, Enum):
    """更正单的状态。更正通过后替换原记录，旧版本保留为历史。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# 允许的状态转换。键为当前状态，值为可以迁移到的状态集合。
ALLOWED_TRANSITIONS: dict[RecordState, set[RecordState]] = {
    RecordState.DRAFT: {RecordState.PENDING},
    RecordState.PENDING: {RecordState.APPROVED, RecordState.REJECTED, RecordState.WITHDRAWN},
    RecordState.APPROVED: {RecordState.VOIDED},
    RecordState.REJECTED: {RecordState.PENDING},
    RecordState.WITHDRAWN: {RecordState.PENDING},
    RecordState.VOIDED: set(),
}

# 计入统计的状态：只有已审核且未作废的记录参与汇总
EFFECTIVE_STATES = {RecordState.APPROVED}


def new_id(prefix: str) -> str:
    """生成带前缀的短标识，便于日志里一眼看出对象类型。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def today() -> date:
    """集中一处取当天日期，测试里可以打桩替换。"""
    return date.today()


def now() -> datetime:
    """集中一处取当前时间，便于审计字段测试。"""
    return datetime.now()


def parse_amount(raw: object) -> Decimal:
    """把用户输入解析成金额，同时做范围与精度校验。

    需求要求：大于零、最多两位小数、单笔不超过上限；不允许静默四舍五入。
    """
    if isinstance(raw, Decimal):
        value = raw
    else:
        text = str(raw).strip()
        if not text:
            raise DomainError("AMOUNT_EMPTY", "金额不能为空")
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise DomainError("AMOUNT_INVALID", f"金额格式不正确：{raw}") from exc
    if value <= 0:
        raise DomainError("AMOUNT_NOT_POSITIVE", "金额必须大于 0")
    if value > MAX_AMOUNT:
        raise DomainError("AMOUNT_TOO_LARGE", "单笔金额不能超过 999999.99")
    if value.as_tuple().exponent < -2:
        raise DomainError("AMOUNT_PRECISION", "金额最多保留两位小数")
    return value.quantize(CENT)


def parse_occurred_on(raw: object) -> date:
    """校验发生日期：不能晚于今天，也不接受未来日期。"""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw).strip()
        try:
            value = date.fromisoformat(text)
        except ValueError as exc:
            raise DomainError("DATE_INVALID", f"日期格式应为 YYYY-MM-DD：{raw}") from exc
    if value > today():
        raise DomainError("DATE_FUTURE", "发生日期不能晚于今天")
    return value


@dataclass
class User:
    """系统用户。邮箱唯一。"""

    user_id: str
    email: str
    nickname: str
    created_at: datetime = field(default_factory=now)

    @staticmethod
    def create(email: str, nickname: str) -> "User":
        email = email.strip().lower()
        if "@" not in email:
            raise DomainError("EMAIL_INVALID", "邮箱格式不正确")
        display = nickname.strip() or email.split("@")[0]
        return User(user_id=new_id("U"), email=email, nickname=display)


@dataclass
class Ledger:
    """账本。个人账本不参与审核，共享账本需要他人审核。"""

    ledger_id: str
    name: str
    type: LedgerType
    archived: bool = False
    created_at: datetime = field(default_factory=now)

    def ensure_writable(self) -> None:
        """归档后的账本只读，不能再记、审、结算。"""
        if self.archived:
            raise DomainError("LEDGER_ARCHIVED", "账本已归档，只能查看历史")


@dataclass
class Membership:
    """成员关系。角色变更与退出都保留历史，不做物理删除。"""

    membership_id: str
    ledger_id: str
    user_id: str
    role: RoleType
    joined_at: datetime = field(default_factory=now)
    left_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.left_at is None


@dataclass
class Category:
    """收支类别。停用后不影响历史统计，只禁止新记录选用。"""

    category_id: str
    ledger_id: str
    name: str
    disabled: bool = False


@dataclass
class TransactionRecord:
    """一笔收支记录。状态迁移受 ALLOWED_TRANSITIONS 约束。"""

    record_id: str
    ledger_id: str
    user_id: str
    direction: Direction
    amount: Decimal
    occurred_on: date
    category_id: str
    state: RecordState = RecordState.DRAFT
    txn_no: Optional[str] = None
    operation_id: Optional[str] = None
    remark: str = ""
    version: int = 1
    replaced_by: Optional[str] = None
    void_reason: Optional[str] = None
    reject_reason: Optional[str] = None
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)

    @property
    def effective(self) -> bool:
        """是否计入统计：已审核、未作废、未被新版本替代。"""
        return (
            self.state in EFFECTIVE_STATES
            and self.void_reason is None
            and self.replaced_by is None
        )

    def can_transition_to(self, target: RecordState) -> bool:
        return target in ALLOWED_TRANSITIONS.get(self.state, set())

    def transition(self, target: RecordState) -> None:
        """执行一次状态迁移，非法迁移直接抛错而不是静默忽略。"""
        if not self.can_transition_to(target):
            raise DomainError(
                "STATE_ILLEGAL",
                f"记录不能从 {self.state.value} 变为 {target.value}",
            )
        self.state = target
        self.updated_at = now()


@dataclass
class Correction:
    """更正单。通过后替换原记录，原记录保留并可查。"""

    correction_id: str
    record_id: str
    proposer_id: str
    reason: str
    old_amount: Decimal
    new_amount: Decimal
    new_occurred_on: date
    status: CorrectionStatus = CorrectionStatus.PENDING
    reviewer_id: Optional[str] = None
    review_remark: Optional[str] = None
    created_at: datetime = field(default_factory=now)


@dataclass
class Budget:
    """按月设置的支出预算。收入不抵扣支出。"""

    budget_id: str
    ledger_id: str
    period: str  # YYYY-MM
    amount: Decimal
    overrun_notified: bool = False

    def remaining(self, spent: Decimal) -> Decimal:
        return self.amount - spent

    def is_overrun(self, spent: Decimal) -> bool:
        return spent > self.amount


@dataclass
class SettlementSnapshot:
    """结算快照。冻结当期的分类与成员汇总，重开后保留为历史版本。"""

    period: str
    total_expense: Decimal
    total_income: Decimal
    by_category: dict[str, Decimal]
    by_member: dict[str, Decimal]
    valid: bool = True

    @property
    def net(self) -> Decimal:
        return self.total_income - self.total_expense


@dataclass
class Settlement:
    """账期结算。同一账期同时只有一份有效结算。"""

    settlement_id: str
    ledger_id: str
    period: str
    snapshot: SettlementSnapshot
    locked: bool = True
    version: int = 1
    operator_id: str = ""
    reopened_by: Optional[str] = None
    reopen_reason: Optional[str] = None
    created_at: datetime = field(default_factory=now)


@dataclass
class AuditEntry:
    """审计日志。只追加，不修改，不删除。"""

    log_id: str
    ledger_id: str
    actor_id: str
    action: str
    target: str
    result: str
    before: str = ""
    after: str = ""
    reason: str = ""
    occurred_at: datetime = field(default_factory=now)


@dataclass
class Notification:
    """站内通知。审核结果、账目生效、成员变动、超预算与结算重开都会产生。"""

    notification_id: str
    ledger_id: str
    user_id: str
    kind: str
    body: str
    delivered_at: datetime = field(default_factory=now)
    read: bool = False
