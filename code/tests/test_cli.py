"""命令行的端到端测试：通过 main() 走完整流程。"""

from __future__ import annotations

import json
from pathlib import Path

from ledger.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else None


def test_cli_full_flow(tmp_path: Path, capsys):
    db = str(tmp_path / "cli.db")

    code, owner = run(capsys, "--db", db, "user", "add",
                      "--email", "a@nju.edu.cn", "--name", "李睿豪")
    assert code == 0 and owner["user_id"].startswith("U-")

    code, admin = run(capsys, "--db", db, "user", "add",
                      "--email", "b@nju.edu.cn", "--name", "雷业成")
    code, ledger = run(capsys, "--db", db, "ledger", "create",
                       "--owner", owner["user_id"], "--name", "家庭账本", "--type", "shared")
    assert ledger["type"] == "shared"

    code, invite = run(capsys, "--db", db, "member", "invite",
                       "--ledger", ledger["ledger_id"], "--user", owner["user_id"],
                       "--email", "b@nju.edu.cn", "--role", "admin")
    code, joined = run(capsys, "--db", db, "member", "accept",
                       "--token", invite["token"], "--user", admin["user_id"])
    assert joined["role"] == "admin"

    code, cat = run(capsys, "--db", db, "category", "add",
                    "--ledger", ledger["ledger_id"], "--user", owner["user_id"],
                    "--name", "餐饮")
    code, rec = run(capsys, "--db", db, "record", "add",
                    "--ledger", ledger["ledger_id"], "--user", admin["user_id"],
                    "--direction", "expense", "--amount", "40.00",
                    "--date", "2026-10-05", "--category", cat["category_id"])
    assert rec["state"] == "draft"

    code, submitted = run(capsys, "--db", db, "record", "submit",
                          "--ledger", ledger["ledger_id"], "--user", admin["user_id"],
                          "--record", rec["record_id"], "--operation-id", "OP-CLI-1")
    assert submitted["state"] == "pending"

    code, approved = run(capsys, "--db", db, "record", "approve",
                         "--ledger", ledger["ledger_id"], "--user", owner["user_id"],
                         "--record", rec["record_id"])
    assert approved["state"] == "approved"

    code, summary = run(capsys, "--db", db, "query", "summary",
                        "--ledger", ledger["ledger_id"], "--period", "2026-10")
    assert summary["expense"] == "40.00"


def test_cli_reports_domain_error(tmp_path: Path, capsys):
    db = str(tmp_path / "err.db")
    code, user = run(capsys, "--db", db, "user", "add",
                     "--email", "x@nju.edu.cn", "--name", "测试")
    code, ledger = run(capsys, "--db", db, "ledger", "create",
                       "--owner", user["user_id"], "--name", "空账本", "--type", "shared")
    code, payload = run(capsys, "--db", db, "record", "add",
                        "--ledger", ledger["ledger_id"], "--user", user["user_id"],
                        "--amount", "-5", "--date", "2026-10-05", "--category", "CAT-none")
    assert code == 2
    assert "error" in payload
