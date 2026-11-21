"""性能实测：对照实验一 REQ-030 写的验收条件跑一遍。

REQ-030 的条件是：一个账本 1 万条有效记录，20 个并发客户端发起共 100 次列表查询，
至少 95 次在 1 秒内成功返回，失败与超时计入分母。

运行：python benchmark.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import (  # noqa: E402
    Direction, LedgerService, LedgerType, QueryService, RecordState, RoleType,
    Store, TransactionRecord,
)
from ledger.models import new_id  # noqa: E402
from ledger.stats import RecordFilter  # noqa: E402

DB = "_bench.db"
RECORDS = 10000
QUERIES = 100
THREADS = 20


def build(db_path: str) -> tuple[str, str, str]:
    """造一个账本和一万条已审核记录，返回 (ledger_id, user_id, category_id)。"""
    if Path(db_path).exists():
        os.remove(db_path)
    store = Store(db_path)
    ledgers = LedgerService(store)
    owner = ledgers.register("bench-owner@nju.edu.cn", "所有者")
    member = ledgers.register("bench-member@nju.edu.cn", "成员")
    lg = ledgers.create_ledger(owner.user_id, "压测账本", LedgerType.SHARED)
    inv = ledgers.invite(lg.ledger_id, owner.user_id, "bench-member@nju.edu.cn",
                         RoleType.MEMBER)
    ledgers.accept_invite(inv.token, member.user_id)

    cats = [ledgers.add_category(lg.ledger_id, owner.user_id, name)
            for name in ("餐饮", "交通", "房租", "日用", "娱乐")]

    start = date(2025, 1, 1)
    rows: list[TransactionRecord] = []
    for i in range(RECORDS):
        day = start + timedelta(days=i % 600)
        rows.append(TransactionRecord(
            record_id=new_id("R"),
            ledger_id=lg.ledger_id,
            user_id=(member.user_id if i % 3 else owner.user_id),
            direction=Direction.EXPENSE if i % 7 else Direction.INCOME,
            amount=Decimal((i % 900) + 1).quantize(Decimal("0.01")),
            occurred_on=day,
            category_id=cats[i % len(cats)].category_id,
            state=RecordState.APPROVED,
        ))
    with store.transaction():
        for rec in rows:
            store.add_record(rec)
    store.conn.commit()
    store.close()
    return lg.ledger_id, member.user_id, cats[0].category_id


def run_sequential(db_path: str, ledger_id: str, category_id: str) -> list[float]:
    store = Store(db_path)
    query = QueryService(store)
    timings = []
    for i in range(QUERIES):
        flt = RecordFilter(
            category_id=category_id if i % 2 else None,
            start=date(2025, 3, 1) + timedelta(days=i % 30),
            end=date(2025, 8, 1),
            min_amount=Decimal("10.00"),
            max_amount=Decimal("800.00"),
        )
        t0 = time.perf_counter()
        rows = query.query(ledger_id, flt)
        timings.append(time.perf_counter() - t0)
        _ = len(rows)
    store.close()
    return timings


def run_concurrent(db_path: str, ledger_id: str, category_id: str) -> list[float]:
    """20 个线程各开连接，共发 100 次查询，模拟并发客户端。"""
    timings: list[float] = []
    lock = __import__("threading").Lock()

    def worker(count: int) -> None:
        local = []
        # 复用连接：真实部署里连接是池化的，不会每次查询都重新建表
        store = Store(db_path, init=False)
        query = QueryService(store)
        for i in range(count):
            flt = RecordFilter(start=date(2025, 5, 1), end=date(2025, 9, 1),
                               min_amount=Decimal("5.00"))
            t0 = time.perf_counter()
            query.query(ledger_id, flt)
            local.append(time.perf_counter() - t0)
        store.close()
        with lock:
            timings.extend(local)

    threads = [Thread(target=worker, args=(QUERIES // THREADS,)) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return timings


def _process_worker(job: tuple[str, str, int]) -> list[float]:
    """子进程入口。放在模块顶层是为了 Windows 的 spawn 模式能序列化。"""
    db_path, ledger_id, count = job
    store = Store(db_path, init=False)
    query = QueryService(store)
    local = []
    for _ in range(count):
        flt = RecordFilter(start=date(2025, 5, 1), end=date(2025, 9, 1),
                           min_amount=Decimal("5.00"))
        t0 = time.perf_counter()
        query.query(ledger_id, flt)
        local.append(time.perf_counter() - t0)
    store.close()
    return local


def run_multiprocess(db_path: str, ledger_id: str) -> list[float]:
    """多进程版本：验证并发瓶颈是不是 Python 的 GIL。"""
    from multiprocessing import Pool

    jobs = [(db_path, ledger_id, QUERIES // THREADS)] * THREADS
    with Pool(processes=THREADS) as pool:
        chunks = pool.map(_process_worker, jobs)
    return [t for chunk in chunks for t in chunk]


def report(name: str, timings: list[float]) -> None:
    timings_sorted = sorted(timings)
    p95 = timings_sorted[int(len(timings_sorted) * 0.95) - 1]
    ok = sum(1 for t in timings if t <= 1.0)
    print(f"\n{name}")
    print(f"  查询次数 {len(timings)}")
    print(f"  平均 {statistics.mean(timings) * 1000:.1f} ms")
    print(f"  P95  {p95 * 1000:.1f} ms")
    print(f"  最大 {max(timings) * 1000:.1f} ms")
    print(f"  1 秒内 {ok} 次，占 {ok / len(timings) * 100:.0f}%")


def main() -> None:
    print(f"准备数据：{RECORDS} 条记录")
    ledger_id, user_id, category_id = build(DB)
    print("数据准备完成")
    report("串行 100 次查询", run_sequential(DB, ledger_id, category_id))
    report(f"并发 {THREADS} 线程共 {QUERIES} 次查询", run_concurrent(DB, ledger_id, category_id))
    report(f"并发 {THREADS} 进程共 {QUERIES} 次查询", run_multiprocess(DB, ledger_id))
    os.remove(DB)


if __name__ == "__main__":
    main()
