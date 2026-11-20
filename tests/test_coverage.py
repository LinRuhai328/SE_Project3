"""补充用例：把命令行、成员管理与统计里此前没走到的分支补上。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger import DomainError, RecordState, RoleType
from ledger.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else None


def test_cli_member_and_ledger_lifecycle(tmp_path: Path, capsys):
    db = str(tmp_path / "a.db")
    _, owner = run(capsys, "--db", db, "user", "add", "--email", "o@nju.edu.cn", "--name", "甲")
    _, other = run(capsys, "--db", db, "user", "add", "--email", "b@nju.edu.cn", "--name", "乙")
    _, lg = run(capsys, "--db", db, "ledger", "create", "--owner", owner["user_id"],
                "--name", "合租", "--type", "shared")
    lid = lg["ledger_id"]

    _, inv = run(capsys, "--db", db, "member", "invite", "--ledger", lid,
                 "--user", owner["user_id"], "--email", "b@nju.edu.cn", "--role", "member")
    run(capsys, "--db", db, "member", "accept", "--token", inv["token"], "--user", other["user_id"])

    code, members = run(capsys, "--db", db, "member", "list", "--ledger", lid)
    assert code == 0 and len(members) == 2

    code, changed = run(capsys, "--db", db, "member", "role", "--ledger", lid,
                        "--user", owner["user_id"], "--target", other["user_id"],
                        "--role", "admin")
    assert changed["role"] == "admin"

    code, _ = run(capsys, "--db", db, "ledger", "archive", "--ledger", lid,
                  "--user", owner["user_id"])
    assert code == 0
    code, restored = run(capsys, "--db", db, "ledger", "restore", "--ledger", lid,
                         "--user", owner["user_id"])
    assert restored["archived"] is False

    code, _ = run(capsys, "--db", db, "member", "leave", "--ledger", lid,
                  "--user", other["user_id"])
    assert code == 0


def test_cli_record_review_chain(tmp_path: Path, capsys):
    db = str(tmp_path / "b.db")
    _, owner = run(capsys, "--db", db, "user", "add", "--email", "a@nju.edu.cn", "--name", "甲")
    _, admin = run(capsys, "--db", db, "user", "add", "--email", "b@nju.edu.cn", "--name", "乙")
    _, lg = run(capsys, "--db", db, "ledger", "create", "--owner", owner["user_id"],
                "--name", "家", "--type", "shared")
    lid = lg["ledger_id"]
    _, inv = run(capsys, "--db", db, "member", "invite", "--ledger", lid,
                 "--user", owner["user_id"], "--email", "b@nju.edu.cn", "--role", "admin")
    run(capsys, "--db", db, "member", "accept", "--token", inv["token"], "--user", admin["user_id"])
    _, cat = run(capsys, "--db", db, "category", "add", "--ledger", lid,
                 "--user", owner["user_id"], "--name", "餐饮")

    _, rec = run(capsys, "--db", db, "record", "add", "--ledger", lid,
                 "--user", admin["user_id"], "--amount", "30.00",
                 "--date", "2026-10-06", "--category", cat["category_id"])
    rid = rec["record_id"]
    run(capsys, "--db", db, "record", "submit", "--ledger", lid,
        "--user", admin["user_id"], "--record", rid, "--operation-id", "OP-A")

    _, pending = run(capsys, "--db", db, "record", "pending", "--ledger", lid,
                     "--user", owner["user_id"])
    assert len(pending) == 1

    _, rejected = run(capsys, "--db", db, "record", "reject", "--ledger", lid,
                      "--user", owner["user_id"], "--record", rid, "--reason", "金额不对")
    assert rejected["state"] == "rejected"

    run(capsys, "--db", db, "record", "submit", "--ledger", lid,
        "--user", admin["user_id"], "--record", rid, "--operation-id", "OP-B")
    _, approved = run(capsys, "--db", db, "record", "approve", "--ledger", lid,
                      "--user", owner["user_id"], "--record", rid, "--expect", "pending")
    assert approved["state"] == "approved"

    _, corr = run(capsys, "--db", db, "record", "correct", "--ledger", lid,
                  "--user", admin["user_id"], "--record", rid, "--amount", "35.00",
                  "--date", "2026-10-06", "--reason", "补记包装费")
    _, new_rec = run(capsys, "--db", db, "record", "approve-correction", "--ledger", lid,
                     "--user", owner["user_id"], "--correction", corr["correction_id"])
    assert new_rec["amount"] == "35.00"

    _, history = run(capsys, "--db", db, "record", "history", "--ledger", lid,
                     "--record", rid)
    assert len(history) == 2

    _, voided = run(capsys, "--db", db, "record", "void", "--ledger", lid,
                    "--user", owner["user_id"], "--record", new_rec["record_id"],
                    "--reason", "重复录入")
    assert voided["state"] == "voided"


def test_cli_query_and_settle(tmp_path: Path, capsys):
    db = str(tmp_path / "c.db")
    _, owner = run(capsys, "--db", db, "user", "add", "--email", "a@nju.edu.cn", "--name", "甲")
    _, lg = run(capsys, "--db", db, "ledger", "create", "--owner", owner["user_id"],
                "--name", "个人", "--type", "personal")
    lid = lg["ledger_id"]
    _, cat = run(capsys, "--db", db, "category", "add", "--ledger", lid,
                 "--user", owner["user_id"], "--name", "交通")
    _, rec = run(capsys, "--db", db, "record", "add", "--ledger", lid,
                 "--user", owner["user_id"], "--amount", "12.00",
                 "--date", "2026-10-07", "--category", cat["category_id"])
    run(capsys, "--db", db, "record", "submit", "--ledger", lid,
        "--user", owner["user_id"], "--record", rec["record_id"])

    code, rows = run(capsys, "--db", db, "query", "list", "--ledger", lid,
                     "--start", "2026-10-01", "--end", "2026-10-31",
                     "--min", "1", "--max", "100")
    assert code == 0 and len(rows) == 1

    _, by_cat = run(capsys, "--db", db, "query", "by-category", "--ledger", lid,
                    "--period", "2026-10")
    assert by_cat[0]["category"] == "交通"

    _, by_member = run(capsys, "--db", db, "query", "by-member", "--ledger", lid,
                       "--period", "2026-10")
    assert by_member[0]["amount"] == "12.00"

    run(capsys, "--db", db, "budget", "set", "--ledger", lid, "--user", owner["user_id"],
        "--period", "2026-10", "--amount", "500.00")
    _, status = run(capsys, "--db", db, "query", "budget", "--ledger", lid,
                    "--period", "2026-10")
    assert status["has_budget"] is True

    _, settled = run(capsys, "--db", db, "settle", "run", "--ledger", lid,
                     "--user", owner["user_id"], "--period", "2026-10",
                     "--today", "2026-11-04")
    assert settled["created"] is True

    _, hist = run(capsys, "--db", db, "settle", "history", "--ledger", lid,
                  "--period", "2026-10")
    assert len(hist) == 1

    _, reopened = run(capsys, "--db", db, "settle", "reopen", "--ledger", lid,
                      "--user", owner["user_id"], "--period", "2026-10",
                      "--reason", "漏记停车费")
    assert reopened["locked"] is False

    _, audit = run(capsys, "--db", db, "query", "audit", "--ledger", lid,
                   "--user", owner["user_id"], "--limit", "5")
    assert len(audit) <= 5


def test_cli_category_disable(tmp_path: Path, capsys):
    db = str(tmp_path / "d.db")
    _, owner = run(capsys, "--db", db, "user", "add", "--email", "a@nju.edu.cn", "--name", "甲")
    _, lg = run(capsys, "--db", db, "ledger", "create", "--owner", owner["user_id"],
                "--name", "家", "--type", "personal")
    lid = lg["ledger_id"]
    _, cat = run(capsys, "--db", db, "category", "add", "--ledger", lid,
                 "--user", owner["user_id"], "--name", "旧类别")
    code, _ = run(capsys, "--db", db, "category", "disable", "--ledger", lid,
                  "--user", owner["user_id"], "--category", cat["category_id"])
    assert code == 0
    code, payload = run(capsys, "--db", db, "record", "add", "--ledger", lid,
                        "--user", owner["user_id"], "--amount", "9.00",
                        "--date", "2026-10-08", "--category", cat["category_id"])
    assert code == 2 and payload["error"] == "CATEGORY_DISABLED"


# ---------------------------------------------------------------- 服务层


def test_revoke_invite_makes_it_unusable(w):
    inv = w.ledgers.invite(w.ledger.ledger_id, w.owner.user_id, "out@nju.edu.cn")
    w.ledgers.revoke_invite(inv.token, w.owner.user_id)
    with pytest.raises(DomainError) as exc:
        w.ledgers.accept_invite(inv.token, w.outsider.user_id)
    assert exc.value.code == "INVITE_EXPIRED"


def test_invite_unknown_token(w):
    with pytest.raises(DomainError) as exc:
        w.ledgers.accept_invite("nope", w.outsider.user_id)
    assert exc.value.code == "INVITE_NOT_FOUND"


def test_reject_correction(w):
    rec = w.approved()
    corr = w.records.propose_correction(w.ledger.ledger_id, w.admin.user_id, rec.record_id,
                                       "50.00", date(2026, 10, 5), "试试")
    result = w.records.reject_correction(w.ledger.ledger_id, w.owner.user_id,
                                         corr.correction_id, "不同意")
    assert result.status.value == "rejected"
    with pytest.raises(DomainError):
        w.records.reject_correction(w.ledger.ledger_id, w.owner.user_id,
                                    corr.correction_id, "再来一次")


def test_withdraw_other_users_record(w):
    rec = w.draft()
    w.records.submit(w.ledger.ledger_id, w.member.user_id, rec.record_id)
    with pytest.raises(DomainError) as exc:
        w.records.withdraw(w.ledger.ledger_id, w.admin.user_id, rec.record_id)
    assert exc.value.code == "NOT_SUBMITTER"


def test_correct_non_approved_record(w):
    rec = w.draft()
    with pytest.raises(DomainError) as exc:
        w.records.propose_correction(w.ledger.ledger_id, w.admin.user_id, rec.record_id,
                                     "50.00", date(2026, 10, 5), "改")
    assert exc.value.code == "STATE_ILLEGAL"


def test_query_one_day_and_unknown_ledger(w):
    from ledger import QueryService

    w.approved(amount="11.00", day=date(2026, 10, 9))
    rows = w.query.query_one_day(w.ledger.ledger_id, date(2026, 10, 9))
    assert len(rows) == 1
    with pytest.raises(DomainError) as exc:
        w.query.audit_for_member(w.ledger.ledger_id, w.outsider.user_id)
    assert exc.value.code == "NOT_MEMBER"


def test_invalid_period_format(w):
    with pytest.raises(DomainError) as exc:
        w.query.monthly_summary(w.ledger.ledger_id, "2026/10")
    assert exc.value.code == "PERIOD_INVALID"


def test_settle_by_member_denied(w):
    w.approved()
    with pytest.raises(DomainError):
        w.settle.settle(w.ledger.ledger_id, w.member.user_id, "2026-10",
                        today=date(2026, 11, 4))


def test_reopen_not_settled(w):
    with pytest.raises(DomainError) as exc:
        w.settle.reopen(w.ledger.ledger_id, w.owner.user_id, "2026-09", "还没结算")
    assert exc.value.code == "NOT_SETTLED"


def test_budget_on_locked_period(w):
    w.approved()
    w.settle.settle(w.ledger.ledger_id, w.admin.user_id, "2026-10", today=date(2026, 11, 4))
    with pytest.raises(DomainError) as exc:
        w.budget.set_monthly(w.ledger.ledger_id, w.owner.user_id, "2026-10", "100.00",
                             today=date(2026, 11, 5))
    assert exc.value.code == "PERIOD_LOCKED"


def test_store_helpers(w):
    from ledger.models import Category, new_id

    cat = Category(new_id("CAT"), w.ledger.ledger_id, "临时")
    w.store.add_category(cat)
    assert w.store.find_category(w.ledger.ledger_id, "临时") is not None
    assert w.store.get_category("CAT-missing") is None
    assert w.store.get_user("U-missing") is None
    assert w.store.get_ledger("LG-missing") is None
    assert w.store.get_record("R-missing") is None
    assert w.store.get_invite("T-missing") is None
    assert w.store.find_user_by_email("nobody@nju.edu.cn") is None


def test_cli_error_carries_hint(tmp_path: Path, capsys):
    db = str(tmp_path / "hint.db")
    code, payload = run(capsys, "--db", db, "ledger", "create",
                        "--owner", "U-none", "--name", "演示")
    assert code == 2
    assert payload["error"] == "USER_NOT_FOUND"
    assert "hint" in payload
