"""Problem reports and ideas from the apps (migration 023). No AI.

  POST   /api/v1/feedback          area.profile  send a report        -> 201 Report
  GET    /api/v1/feedback/mine     area.profile  my reports + status  -> {"items": [Report], "count"}
  GET    /api/v1/feedback          area.admin    all reports (owner)  -> {"items": [OwnerReport], "count"}
                                                 ?status=open|in_progress|fixed|wont_fix|duplicate&limit=1..200
  PATCH  /api/v1/feedback/{id}     area.admin    {status?, owner_note?} -> OwnerReport

POST body (only "message" is required):
  {"kind": "bug" | "idea" | "other" (default bug),
   "message": str (1-4000),
   "platform": "ios" | "web" (default ios),
   "screen": str?          # where it happened, e.g. "Holdings", "Stock XEQT.TO"
   "app_version": str?, "build": str?, "os_version": str?, "device_model": str?, "locale": str?,
   "diagnostics": object?  # <= 16 KB: e.g. {"server_version", "sentry_event_id",
                           #   "last_request": {"path", "status", "code"}, "network": "wifi"}
  }
  Never send passwords, tokens or full account numbers in diagnostics.

Report:      {"id", "kind", "message", "platform", "screen", "app_version", "build",
              "status": "open" | "in_progress" | "fixed" | "wont_fix" | "duplicate",
              "created_at", "resolved_at" | null}
OwnerReport: Report + {"user_id", "os_version", "device_model", "locale", "diagnostics",
              "owner_note", "updated_at"}

The owner gets a Telegram message for each new report. Rate limit: 3 reports
per 5 minutes per user. Errors: 422 invalid_message | invalid_kind |
invalid_platform | invalid_diagnostics | invalid_<field> | invalid_status |
invalid_owner_note | nothing_to_update, 404 report_not_found,
503 migration_required {"migration": "023_feedback_reports.sql"}.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict

from app.core.access import require_feature
from app.core.api_errors import api_error, run_db_for
from app.core.dependencies import get_current_user
from app.services import feedback as svc

router = APIRouter(prefix="/feedback", tags=["Feedback"])


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Optional[Any] = None
    message: Optional[Any] = None
    platform: Optional[Any] = None
    screen: Optional[Any] = None
    app_version: Optional[Any] = None
    build: Optional[Any] = None
    os_version: Optional[Any] = None
    device_model: Optional[Any] = None
    locale: Optional[Any] = None
    diagnostics: Optional[Any] = None


class ReportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Any] = None
    owner_note: Optional[Any] = None


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_feature("area.profile"))])
async def send_report(body: ReportIn, user: dict = Depends(get_current_user)):
    row = svc.clean_report(body.model_dump())
    saved = await run_db_for(svc.MIGRATION, svc.create, user["user_id"], row)
    asyncio.get_running_loop().run_in_executor(None, svc.notify_owner, saved, user.get("username"))
    return {k: saved.get(k) for k in svc.PUBLIC_COLUMNS.split(", ")}


@router.get("/mine", dependencies=[Depends(require_feature("area.profile"))])
async def my_reports(user: dict = Depends(get_current_user)):
    items = await run_db_for(svc.MIGRATION, svc.list_mine, user["user_id"])
    return {"items": items, "count": len(items)}


@router.get("", dependencies=[Depends(require_feature("area.admin"))])
async def all_reports(
    status_: Optional[Literal["open", "in_progress", "fixed", "wont_fix", "duplicate"]] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=200),
):
    items = await run_db_for(svc.MIGRATION, svc.list_all, status_, limit)
    return {"items": items, "count": len(items)}


@router.patch("/{report_id}", dependencies=[Depends(require_feature("area.admin"))])
async def update_report(report_id: UUID, body: ReportUpdate):
    data = svc.clean_update(body.model_dump(exclude_unset=True))
    saved = await run_db_for(svc.MIGRATION, svc.update, str(report_id), data)
    if not saved:
        raise api_error("report_not_found", "No report with that id.", 404)
    return saved
