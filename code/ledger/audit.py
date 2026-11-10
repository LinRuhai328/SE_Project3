"""审计与通知：写操作统一在这里留痕。

审计先于通知落库。这样即使通知发送失败，日志也已经记下来，
不会出现“账改了、日志没写”的半截状态（实验一 REQ-021 的要求）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import AuditEntry, DomainError, Notification, new_id, now
from .store import Store


def _brief(payload: Optional[dict[str, Any]]) -> str:
    """把对象压成一行 JSON，保留关键字段，避免日志里塞整条记录。"""
    if not payload:
        return ""
    keep = {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
            for k, v in payload.items() if not k.startswith("_")}
    return json.dumps(keep, ensure_ascii=False, sort_keys=True)


def record(store: Store, ledger_id: str, actor_id: str, action: str, target: str,
           result: str = "ok", before: Optional[dict[str, Any]] = None,
           after: Optional[dict[str, Any]] = None, reason: str = "") -> AuditEntry:
    """追加一条审计。日志只追加，不提供修改与删除接口。"""
    entry = AuditEntry(
        log_id=new_id("L"),
        ledger_id=ledger_id,
        actor_id=actor_id,
        action=action,
        target=target,
        result=result,
        before=_brief(before),
        after=_brief(after),
        reason=reason,
        occurred_at=now(),
    )
    store.add_audit(entry)
    return entry


def notify(store: Store, ledger_id: str, user_id: str, kind: str, body: str) -> Notification:
    """站内通知。第一版只做站内，不接外部渠道。"""
    note = Notification(
        notification_id=new_id("N"),
        ledger_id=ledger_id,
        user_id=user_id,
        kind=kind,
        body=body,
        delivered_at=now(),
    )
    store.add_notification(note)
    return note


def notify_members(store: Store, ledger_id: str, kind: str, body: str,
                   exclude: Optional[str] = None) -> int:
    """给账本当前成员群发，退出的人不再收到。"""
    count = 0
    for m in store.list_memberships(ledger_id, active_only=True):
        if exclude and m.user_id == exclude:
            continue
        notify(store, ledger_id, m.user_id, kind, body)
        count += 1
    return count


def assert_reason(reason: str) -> str:
    """驳回、作废、重开都必须写原因，空字符串一律拒绝。"""
    text = (reason or "").strip()
    if not text:
        raise DomainError("REASON_REQUIRED", "该操作必须填写原因")
    if len(text) > 200:
        raise DomainError("REASON_TOO_LONG", "原因最多 200 字")
    return text
