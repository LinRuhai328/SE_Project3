"""多人共享记账系统的领域层。

模块划分与实验二的类图一一对应：

- models      实体与枚举，不含存储逻辑
- store       SQLite 表结构与读写映射
- permissions 角色到动作的权限矩阵
- records     记录状态机、审核、更正、作废
- stats       多条件查询与统计汇总
- settlement  预算、结算、重开与快照
- ledger_service 账本与成员关系
- audit       审计与通知
- cli         命令行入口
"""

from .models import (  # noqa: F401
    Budget,
    Category,
    Correction,
    CorrectionStatus,
    Direction,
    DomainError,
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
from .store import Store  # noqa: F401
from .ledger_service import LedgerService  # noqa: F401
from .records import RecordService, SubmitResult  # noqa: F401
from .settlement import BudgetService, SettlementService, SettlementResult  # noqa: F401
from .stats import QueryService, RecordFilter, Summary  # noqa: F401

__all__ = [
    "Budget",
    "BudgetService",
    "Category",
    "Correction",
    "CorrectionStatus",
    "Direction",
    "DomainError",
    "Ledger",
    "LedgerService",
    "LedgerType",
    "Membership",
    "Notification",
    "QueryService",
    "RecordFilter",
    "RecordService",
    "RecordState",
    "RoleType",
    "Settlement",
    "SettlementService",
    "SettlementSnapshot",
    "Store",
    "SubmitResult",
    "Summary",
    "TransactionRecord",
    "User",
]
