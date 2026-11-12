"""命令行入口。

实验三不要求图形界面，用命令行把主流程走一遍即可：
注册、建账本、记账、提交、审核、查询、预算与结算。

用法示例：
    python -m ledger.cli init --db ledger.db
    python -m ledger.cli user add --email a@nju.edu.cn --name 李睿豪
    python -m ledger.cli ledger create --owner U-xxx --name 家庭账本 --type shared
    python -m ledger.cli record add --ledger LG-xxx --user U-yyy --direction expense \
        --amount 40.00 --date 2026-10-05 --category CAT-zzz
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal

from . import (
    BudgetService,
    Direction,
    DomainError,
    LedgerService,
    LedgerType,
    QueryService,
    RecordService,
    RecordState,
    RoleType,
    SettlementService,
    Store,
)
from .stats import RecordFilter


def _services(db_path: str):
    store = Store(db_path)
    return {
        "store": store,
        "ledger": LedgerService(store),
        "record": RecordService(store),
        "query": QueryService(store),
        "budget": BudgetService(store),
        "settle": SettlementService(store),
    }


def _print(payload: object) -> None:
    """统一输出 JSON，方便重定向到文件或在测试里解析。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- 用户
def cmd_user(args, svc) -> int:
    if args.action == "add":
        user = svc["ledger"].register(args.email, args.name)
        _print({"user_id": user.user_id, "email": user.email, "nickname": user.nickname})
    elif args.action == "list":
        rows = [{"user_id": u.user_id, "email": u.email, "nickname": u.nickname}
                for u in svc["store"].list_users()]
        _print(rows)
    elif args.action == "login":
        user = svc["ledger"].login(args.email)
        _print({"user_id": user.user_id, "nickname": user.nickname})
    return 0


# ---------------------------------------------------------------- 账本
def cmd_ledger(args, svc) -> int:
    if args.action == "create":
        ledger = svc["ledger"].create_ledger(
            args.owner, args.name, LedgerType(args.type))
        _print({"ledger_id": ledger.ledger_id, "name": ledger.name, "type": ledger.type.value})
    elif args.action == "list":
        rows = [{"ledger_id": lg.ledger_id, "name": lg.name,
                 "type": lg.type.value, "archived": lg.archived}
                for lg in svc["store"].list_ledgers()]
        _print(rows)
    elif args.action == "archive":
        ledger = svc["ledger"].archive(args.ledger, args.user)
        _print({"ledger_id": ledger.ledger_id, "archived": ledger.archived})
    elif args.action == "restore":
        ledger = svc["ledger"].restore(args.ledger, args.user)
        _print({"ledger_id": ledger.ledger_id, "archived": ledger.archived})
    return 0


# ---------------------------------------------------------------- 成员
def cmd_member(args, svc) -> int:
    if args.action == "invite":
        invite = svc["ledger"].invite(args.ledger, args.user, args.email,
                                      RoleType(args.role))
        _print({"token": invite.token, "expires_at": invite.expires_at.isoformat()})
    elif args.action == "accept":
        m = svc["ledger"].accept_invite(args.token, args.user)
        _print({"membership_id": m.membership_id, "role": m.role.value})
    elif args.action == "list":
        rows = [{"user_id": m.user_id, "role": m.role.value,
                 "joined_at": m.joined_at.isoformat()}
                for m in svc["store"].list_memberships(args.ledger)]
        _print(rows)
    elif args.action == "role":
        m = svc["ledger"].change_role(args.ledger, args.user, args.target, RoleType(args.role))
        _print({"user_id": m.user_id, "role": m.role.value})
    elif args.action == "transfer":
        svc["ledger"].transfer_ownership(args.ledger, args.user, args.target)
        _print({"transferred": True})
    elif args.action == "leave":
        svc["ledger"].leave(args.ledger, args.user)
        _print({"left": True})
    return 0


# ---------------------------------------------------------------- 类别
def cmd_category(args, svc) -> int:
    if args.action == "add":
        cat = svc["ledger"].add_category(args.ledger, args.user, args.name)
        _print({"category_id": cat.category_id, "name": cat.name})
    elif args.action == "list":
        rows = [{"category_id": c.category_id, "name": c.name, "disabled": c.disabled}
                for c in svc["store"].list_categories(args.ledger)]
        _print(rows)
    elif args.action == "disable":
        svc["ledger"].disable_category(args.ledger, args.user, args.category)
        _print({"disabled": True})
    return 0


# ---------------------------------------------------------------- 记录
def cmd_record(args, svc) -> int:
    if args.action == "add":
        rec = svc["record"].create_draft(
            args.ledger, args.user, Direction(args.direction), args.amount,
            args.date, args.category, args.remark or "", args.txn_no)
        _print({"record_id": rec.record_id, "state": rec.state.value, "txn_no": rec.txn_no})
    elif args.action == "submit":
        result = svc["record"].submit(args.ledger, args.user, args.record, args.operation_id)
        _print({"record_id": result.record.record_id, "state": result.record.state.value,
                "repeated": result.repeated, "message": result.message})
    elif args.action == "approve":
        rec = svc["record"].approve(args.ledger, args.user, args.record,
                                    RecordState(args.expect) if args.expect else None)
        _print({"record_id": rec.record_id, "state": rec.state.value})
    elif args.action == "reject":
        rec = svc["record"].reject(args.ledger, args.user, args.record, args.reason)
        _print({"record_id": rec.record_id, "state": rec.state.value,
                "reason": rec.reject_reason})
    elif args.action == "withdraw":
        rec = svc["record"].withdraw(args.ledger, args.user, args.record)
        _print({"record_id": rec.record_id, "state": rec.state.value})
    elif args.action == "void":
        rec = svc["record"].void(args.ledger, args.user, args.record, args.reason)
        _print({"record_id": rec.record_id, "state": rec.state.value})
    elif args.action == "correct":
        correction = svc["record"].propose_correction(
            args.ledger, args.user, args.record, args.amount, args.date, args.reason)
        _print({"correction_id": correction.correction_id,
                "status": correction.status.value})
    elif args.action == "approve-correction":
        rec = svc["record"].approve_correction(args.ledger, args.user, args.correction)
        _print({"record_id": rec.record_id, "version": rec.version, "amount": str(rec.amount)})
    elif args.action == "pending":
        rows = [{"record_id": r.record_id, "amount": str(r.amount), "user": r.user_id}
                for r in svc["record"].list_pending(args.ledger, args.user)]
        _print(rows)
    elif args.action == "history":
        rows = [{"record_id": r.record_id, "version": r.version, "amount": str(r.amount),
                 "state": r.state.value} for r in svc["record"].history_of(args.record)]
        _print(rows)
    return 0


# ---------------------------------------------------------------- 查询
def cmd_query(args, svc) -> int:
    if args.action == "list":
        flt = RecordFilter(
            category_id=args.category,
            member_id=args.member,
            start=date.fromisoformat(args.start) if args.start else None,
            end=date.fromisoformat(args.end) if args.end else None,
            min_amount=Decimal(args.min) if args.min else None,
            max_amount=Decimal(args.max) if args.max else None,
            include_non_effective=args.include_non_effective,
        )
        rows = [{"record_id": r.record_id, "date": r.occurred_on.isoformat(),
                 "amount": str(r.amount), "state": r.state.value,
                 "user": r.user_id, "category": r.category_id}
                for r in svc["query"].query(args.ledger, flt)]
        _print(rows)
    elif args.action == "summary":
        s = svc["query"].monthly_summary(args.ledger, args.period)
        _print({"period": args.period, "expense": str(s.expense), "income": str(s.income),
                "net": str(s.net), "count": s.count})
    elif args.action == "by-category":
        _print([{"category": n, "amount": str(a), "count": c}
                for n, a, c in svc["query"].by_category(args.ledger, args.period)])
    elif args.action == "by-member":
        _print([{"member": m, "amount": str(a), "count": c}
                for m, a, c in svc["query"].by_member(args.ledger, args.period)])
    elif args.action == "budget":
        _print(svc["query"].budget_status(args.ledger, args.period))
    elif args.action == "audit":
        _print(svc["query"].audit_for_member(args.ledger, args.user, args.limit))
    return 0


# ---------------------------------------------------------------- 预算与结算
def cmd_budget(args, svc) -> int:
    budget = svc["budget"].set_monthly(args.ledger, args.user, args.period, args.amount)
    _print({"period": budget.period, "amount": str(budget.amount)})
    return 0


def cmd_settle(args, svc) -> int:
    if args.action == "run":
        result = svc["settle"].settle(args.ledger, args.user, args.period,
                                      today=date.fromisoformat(args.today) if args.today else None)
        _print({"settlement_id": result.settlement.settlement_id,
                "created": result.created,
                "expense": str(result.settlement.snapshot.total_expense),
                "income": str(result.settlement.snapshot.total_income),
                "message": result.message})
    elif args.action == "reopen":
        s = svc["settle"].reopen(args.ledger, args.user, args.period, args.reason)
        _print({"settlement_id": s.settlement_id, "locked": s.locked,
                "reason": s.reopen_reason})
    elif args.action == "history":
        _print(svc["settle"].history(args.ledger, args.period))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="多人共享记账系统命令行")
    parser.add_argument("--db", default="ledger.db", help="SQLite 数据库文件")
    sub = parser.add_subparsers(dest="command", required=True)

    p_user = sub.add_parser("user", help="用户")
    p_user.add_argument("action", choices=["add", "list", "login"])
    p_user.add_argument("--email")
    p_user.add_argument("--name")

    p_ledger = sub.add_parser("ledger", help="账本")
    p_ledger.add_argument("action", choices=["create", "list", "archive", "restore"])
    p_ledger.add_argument("--owner")
    p_ledger.add_argument("--name")
    p_ledger.add_argument("--type", choices=["personal", "shared"], default="shared")
    p_ledger.add_argument("--ledger")
    p_ledger.add_argument("--user")

    p_member = sub.add_parser("member", help="成员与权限")
    p_member.add_argument("action", choices=[
        "invite", "accept", "list", "role", "transfer", "leave"])
    p_member.add_argument("--ledger")
    p_member.add_argument("--user")
    p_member.add_argument("--email")
    p_member.add_argument("--role", default="member")
    p_member.add_argument("--token")
    p_member.add_argument("--target")

    p_cat = sub.add_parser("category", help="类别")
    p_cat.add_argument("action", choices=["add", "list", "disable"])
    p_cat.add_argument("--ledger")
    p_cat.add_argument("--user")
    p_cat.add_argument("--name")
    p_cat.add_argument("--category")

    p_rec = sub.add_parser("record", help="收支记录")
    p_rec.add_argument("action", choices=[
        "add", "submit", "approve", "reject", "withdraw", "void",
        "correct", "approve-correction", "pending", "history"])
    p_rec.add_argument("--ledger")
    p_rec.add_argument("--user")
    p_rec.add_argument("--record")
    p_rec.add_argument("--direction", choices=["income", "expense"], default="expense")
    p_rec.add_argument("--amount")
    p_rec.add_argument("--date")
    p_rec.add_argument("--category")
    p_rec.add_argument("--remark")
    p_rec.add_argument("--txn-no")
    p_rec.add_argument("--operation-id")
    p_rec.add_argument("--reason")
    p_rec.add_argument("--expect")
    p_rec.add_argument("--correction")

    p_query = sub.add_parser("query", help="查询与统计")
    p_query.add_argument("action", choices=["list", "summary", "by-category", "by-member",
                                            "budget", "audit"])
    p_query.add_argument("--ledger")
    p_query.add_argument("--user")
    p_query.add_argument("--period")
    p_query.add_argument("--category")
    p_query.add_argument("--member")
    p_query.add_argument("--start")
    p_query.add_argument("--end")
    p_query.add_argument("--min")
    p_query.add_argument("--max")
    p_query.add_argument("--include-non-effective", action="store_true")
    p_query.add_argument("--limit", type=int, default=20)

    p_budget = sub.add_parser("budget", help="月度预算")
    p_budget.add_argument("action", choices=["set"])
    p_budget.add_argument("--ledger")
    p_budget.add_argument("--user")
    p_budget.add_argument("--period")
    p_budget.add_argument("--amount")

    p_settle = sub.add_parser("settle", help="结算")
    p_settle.add_argument("action", choices=["run", "reopen", "history"])
    p_settle.add_argument("--ledger")
    p_settle.add_argument("--user")
    p_settle.add_argument("--period")
    p_settle.add_argument("--reason")
    p_settle.add_argument("--today")

    return parser


HANDLERS = {
    "user": cmd_user,
    "ledger": cmd_ledger,
    "member": cmd_member,
    "category": cmd_category,
    "record": cmd_record,
    "query": cmd_query,
    "budget": cmd_budget,
    "settle": cmd_settle,
}

# 模拟用户走流程时发现的问题：只给错误码，用户不知道下一步做什么。
# 这里按错误码补一句可执行的提示，不影响退出码与错误码本身。
HINTS = {
    "USER_NOT_FOUND": "先执行 user add 注册，或用 user list 查看已有标识",
    "LEDGER_NOT_FOUND": "用 ledger list 查看账本标识",
    "CATEGORY_NOT_FOUND": "用 category list 查看类别，或先 category add 新建",
    "RECORD_NOT_FOUND": "用 query list 查看记录标识",
    "NOT_MEMBER": "该账号不在此账本中，需由所有者用 member invite 邀请",
    "PERMISSION_DENIED": "当前角色无此权限，member / admin / owner 权限依次递增",
    "PERIOD_LOCKED": "该月已结算，需所有者执行 settle reopen 后才能修改",
    "PENDING_EXISTS": "先处理待审核记录，或让提交人撤回",
    "AMOUNT_NOT_POSITIVE": "金额必须大于 0，支出与收入用 --direction 区分",
    "DATE_FUTURE": "发生日期不能晚于今天",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    svc = _services(args.db)
    try:
        return HANDLERS[args.command](args, svc)
    except DomainError as exc:
        payload = {"error": exc.code, "message": exc.message}
        hint = HINTS.get(exc.code)
        if hint:
            payload["hint"] = hint
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    finally:
        svc["store"].close()


if __name__ == "__main__":
    sys.exit(main())
