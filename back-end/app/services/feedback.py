"""Problem reports and ideas sent from the apps (migration 023).

A signed-in user sends a report (POST /feedback); it is stored in
`feedback_reports` and the owner gets a short Telegram message (the owner's
sign-in chat, when set). The owner lists and triages reports (status +
note). A user can list their own reports and see the status.

Validation is here (pure `clean_report` / `clean_update`), storage below.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from typing import Any

from loguru import logger

from app.core.api_errors import api_error
from app.db.supabase import get_client

MIGRATION = "023_feedback_reports.sql"
KINDS = ("bug", "idea", "other")
PLATFORMS = ("ios", "web")
STATUSES = ("open", "in_progress", "fixed", "wont_fix", "duplicate")
CLOSED = ("fixed", "wont_fix", "duplicate")
MESSAGE_MAX = 4000
NOTE_MAX = 2000
DIAGNOSTICS_MAX_BYTES = 16_000
SHORT_FIELDS = {"screen": 80, "app_version": 32, "build": 32, "os_version": 32,
                "device_model": 64, "locale": 16}
PUBLIC_COLUMNS = "id, kind, message, platform, screen, app_version, build, status, created_at, resolved_at"
OWNER_COLUMNS = ("id, user_id, kind, message, platform, screen, app_version, build, os_version, "
                 "device_model, locale, diagnostics, status, owner_note, created_at, updated_at, resolved_at")


def _invalid(code: str, message: str):
    return api_error(code, message, 422)


def _short(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"invalid_{name}", f"{name} must be text.")
    v = value.strip()
    return v[:SHORT_FIELDS[name]] or None


def clean_report(body: dict) -> dict:
    """Validated row for a new report (raises 422). Pure."""
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _invalid("invalid_message", "Describe the problem.")
    message = message.strip()
    if len(message) > MESSAGE_MAX:
        raise _invalid("invalid_message", f"Keep it under {MESSAGE_MAX} characters.")
    kind = body.get("kind") or "bug"
    if kind not in KINDS:
        raise _invalid("invalid_kind", f"kind must be one of {', '.join(KINDS)}.")
    platform = body.get("platform") or "ios"
    if platform not in PLATFORMS:
        raise _invalid("invalid_platform", f"platform must be one of {', '.join(PLATFORMS)}.")
    diagnostics = body.get("diagnostics")
    if diagnostics is not None:
        if not isinstance(diagnostics, dict):
            raise _invalid("invalid_diagnostics", "diagnostics must be an object.")
        if len(json.dumps(diagnostics, default=str)) > DIAGNOSTICS_MAX_BYTES:
            raise _invalid("invalid_diagnostics", f"diagnostics must be under {DIAGNOSTICS_MAX_BYTES} bytes.")
    row = {"kind": kind, "message": message, "platform": platform, "diagnostics": diagnostics}
    for name in SHORT_FIELDS:
        row[name] = _short(name, body.get(name))
    return row


def clean_update(body: dict) -> dict:
    """Validated owner update {status?, owner_note?} (raises 422). Pure."""
    out: dict = {}
    if "status" in body:
        if body["status"] not in STATUSES:
            raise _invalid("invalid_status", f"status must be one of {', '.join(STATUSES)}.")
        out["status"] = body["status"]
        out["resolved_at"] = datetime.now(timezone.utc).isoformat() if body["status"] in CLOSED else None
    if "owner_note" in body:
        note = body["owner_note"]
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
            raise _invalid("invalid_owner_note", f"owner_note must be text under {NOTE_MAX} characters.")
        out["owner_note"] = (note.strip() or None) if isinstance(note, str) else None
    if not out:
        raise _invalid("nothing_to_update", "Send status and/or owner_note.")
    return out


def owner_message(report: dict, username: str | None) -> str:
    """Telegram text for the owner. Pure."""
    where = " · ".join(x for x in (report.get("platform"), report.get("app_version"),
                                   report.get("screen")) if x)
    text = report.get("message") or ""
    if len(text) > 500:
        text = text[:500] + "…"
    return (f"🐞 <b>New {escape(report.get('kind') or 'bug')} report</b> from "
            f"{escape(username or 'a user')}\n<i>{escape(where)}</i>\n\n{escape(text)}")


# ---------------------------------------------------------------- storage (blocking)

def create(user_id: str, row: dict) -> dict:
    res = get_client().table("feedback_reports").insert({**row, "user_id": user_id}).execute()
    return (res.data or [{}])[0]


def list_mine(user_id: str, limit: int = 50) -> list[dict]:
    return (get_client().table("feedback_reports").select(PUBLIC_COLUMNS)
            .eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute().data or [])


def list_all(status: str | None = None, limit: int = 100) -> list[dict]:
    q = get_client().table("feedback_reports").select(OWNER_COLUMNS)
    if status:
        q = q.eq("status", status)
    return q.order("created_at", desc=True).limit(limit).execute().data or []


def update(report_id: str, data: dict) -> dict | None:
    res = get_client().table("feedback_reports").update(data).eq("id", report_id).execute()
    return (res.data or [None])[0]


def notify_owner(report: dict, username: str | None) -> None:
    """Telegram to every owner with a sign-in chat. Never raises."""
    try:
        from app.notifications.telegram_bot import enqueue
        owners = (get_client().table("users").select("telegram_chat_id")
                  .eq("access_level", "owner").execute().data or [])
        for o in owners:
            if o.get("telegram_chat_id"):
                enqueue(str(o["telegram_chat_id"]), owner_message(report, username), urgent=True)
    except Exception as e:
        logger.warning(f"feedback: owner notification failed: {type(e).__name__}")
