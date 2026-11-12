"""测试夹具：每个用例建一个内存库，避免用例之间互相污染。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import (  # noqa: E402
    BudgetService, LedgerService, LedgerType, QueryService, RecordService,
    RoleType, SettlementService, Store,
)


class World:
    """一组常用对象：三个人、一个共享账本、两个类别和常用服务。"""

    def __init__(self) -> None:
        self.store = Store(":memory:")
        self.ledgers = LedgerService(self.store)
        self.records = RecordService(self.store)
        self.query = QueryService(self.store)
        self.budget = BudgetService(self.store)
        self.settle = SettlementService(self.store)

        self.owner = self.ledgers.register("owner@nju.edu.cn", "李睿豪")
        self.admin = self.ledgers.register("admin@nju.edu.cn", "雷业成")
        self.member = self.ledgers.register("member@nju.edu.cn", "王雨桐")
        self.outsider = self.ledgers.register("out@nju.edu.cn", "路人")

        self.ledger = self.ledgers.create_ledger(self.owner.user_id, "家庭账本",
                                                 LedgerType.SHARED)
        inv = self.ledgers.invite(self.ledger.ledger_id, self.owner.user_id,
                                  "admin@nju.edu.cn", RoleType.ADMIN)
        self.ledgers.accept_invite(inv.token, self.admin.user_id)
        inv = self.ledgers.invite(self.ledger.ledger_id, self.owner.user_id,
                                  "member@nju.edu.cn", RoleType.MEMBER)
        self.ledgers.accept_invite(inv.token, self.member.user_id)

        self.food = self.ledgers.add_category(self.ledger.ledger_id, self.owner.user_id, "餐饮")
        self.traffic = self.ledgers.add_category(self.ledger.ledger_id, self.owner.user_id, "交通")

    def draft(self, user=None, amount="40.00", day=date(2026, 10, 5), category=None,
              direction="expense"):
        user = user or self.member
        return self.records.create_draft(
            self.ledger.ledger_id, user.user_id, direction, amount, day,
            (category or self.food).category_id, "测试记录")

    def approved(self, user=None, amount="40.00", day=date(2026, 10, 5), category=None):
        rec = self.draft(user=user, amount=amount, day=day, category=category)
        self.records.submit(self.ledger.ledger_id, rec.user_id, rec.record_id)
        reviewer = self.admin if rec.user_id != self.admin.user_id else self.owner
        return self.records.approve(self.ledger.ledger_id, reviewer.user_id, rec.record_id)

    def close(self) -> None:
        self.store.close()


@pytest.fixture()
def w():
    world = World()
    yield world
    world.close()
