"""端到端演示：把主流程跑一遍并打印结果，输出可直接贴进报告。

运行：python demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import (  # noqa: E402
    BudgetService, Direction, LedgerService, LedgerType, QueryService,
    RecordService, RecordState, RoleType, SettlementService, Store,
)

DB = "_demo.db"
if Path(DB).exists():
    os.remove(DB)

store = Store(DB)
ledgers = LedgerService(store)
records = RecordService(store)
query = QueryService(store)
budget = BudgetService(store)
settle = SettlementService(store)


def title(text: str) -> None:
    print()
    print("=" * 68)
    print(text)
    print("=" * 68)


title("1. 注册用户")
owner = ledgers.register("lrh@smail.nju.edu.cn", "李睿豪")
admin = ledgers.register("lyc@smail.nju.edu.cn", "雷业成")
member = ledgers.register("wyt@smail.nju.edu.cn", "王雨桐")
for u in (owner, admin, member):
    print(f"  注册成功 {u.user_id}  {u.email}  {u.nickname}")

title("2. 建共享账本并邀请成员")
lg = ledgers.create_ledger(owner.user_id, "家庭账本", LedgerType.SHARED)
print(f"  账本 {lg.ledger_id}  类型={lg.type.value}")
inv = ledgers.invite(lg.ledger_id, owner.user_id, "lyc@smail.nju.edu.cn", RoleType.ADMIN)
ledgers.accept_invite(inv.token, admin.user_id)
inv = ledgers.invite(lg.ledger_id, owner.user_id, "wyt@smail.nju.edu.cn", RoleType.MEMBER)
ledgers.accept_invite(inv.token, member.user_id)
for m in store.list_memberships(lg.ledger_id):
    print(f"  成员 {m.user_id}  角色={m.role.value}")

title("3. 记账：新增、提交、幂等重试")
food = ledgers.add_category(lg.ledger_id, owner.user_id, "餐饮")
rec = records.create_draft(lg.ledger_id, member.user_id, Direction.EXPENSE, "40.00",
                           date(2026, 10, 5), food.category_id, "午饭")
print(f"  草稿 {rec.record_id}  金额={rec.amount}  状态={rec.state.value}")
first = records.submit(lg.ledger_id, member.user_id, rec.record_id, "OP-0001")
print(f"  提交 → 状态={first.record.state.value}  重复={first.repeated}")
retry = records.submit(lg.ledger_id, member.user_id, rec.record_id, "OP-0001")
print(f"  网络重试 → 重复={retry.repeated}  返回同一记录={retry.record.record_id == rec.record_id}")

title("4. 审核：禁止自审")
try:
    records.approve(lg.ledger_id, member.user_id, rec.record_id)
except Exception as exc:  # noqa: BLE001
    print(f"  成员自审被拒绝：{exc}")
approved = records.approve(lg.ledger_id, admin.user_id, rec.record_id,
                           expected_state=RecordState.PENDING)
print(f"  管理员审核 → 状态={approved.state.value}")

title("5. 统计")
summary = query.monthly_summary(lg.ledger_id, "2026-10")
print(f"  支出={summary.expense}  收入={summary.income}  净额={summary.net}  笔数={summary.count}")
for name, amount, count in query.by_category(lg.ledger_id, "2026-10"):
    print(f"  分类 {name}: {amount} 元 / {count} 笔")

title("6. 更正：40 → 120 走二次审核")
correction = records.propose_correction(lg.ledger_id, member.user_id, rec.record_id,
                                       "120.00", date(2026, 10, 5), "小票看错金额")
print(f"  更正单 {correction.correction_id}  状态={correction.status.value}")
print(f"  审核前统计仍为 {query.monthly_summary(lg.ledger_id, '2026-10').expense}（更正未生效）")
new_rec = records.approve_correction(lg.ledger_id, owner.user_id, correction.correction_id)
print(f"  更正通过 → 新版本 v{new_rec.version} 金额={new_rec.amount}  "
      f"统计变为 {query.monthly_summary(lg.ledger_id, '2026-10').expense}")

title("7. 预算与结算")
budget.set_monthly(lg.ledger_id, owner.user_id, "2026-10", "500.00")
status = query.budget_status(lg.ledger_id, "2026-10")
print(f"  预算 {status['amount']}  已用 {status['spent']}  剩余 {status['remaining']}")
result = settle.settle(lg.ledger_id, owner.user_id, "2026-10", today=date(2026, 11, 4))
print(f"  结算 {result.settlement.settlement_id}  "
      f"支出 {result.settlement.snapshot.total_expense}  锁定={result.settlement.locked}")

title("8. 结算后写入被拒绝")
try:
    records.create_draft(lg.ledger_id, member.user_id, Direction.EXPENSE, "10.00",
                         date(2026, 10, 9), food.category_id)
except Exception as exc:  # noqa: BLE001
    print(f"  新增被拒绝：{exc}")

title("9. 重开账期")
opened = settle.reopen(lg.ledger_id, owner.user_id, "2026-10", "漏记房租")
print(f"  重开成功，版本 v{opened.version}，原因：{opened.reopen_reason}，"
      f"可写={settle.is_mutable(lg.ledger_id, '2026-10')}")

title("10. 审计日志")
for entry in query.recent_audit(lg.ledger_id, limit=6):
    print(f"  {entry['at']}  {entry['action']:18s} {entry['target']}  "
          f"原因={entry['reason'] or '-'}")

print()
print("演示结束。")
store.close()
