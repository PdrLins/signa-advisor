"""Problem reports from the apps (migration 023): POST /feedback, my reports,
owner triage, validation, rate tier and the owner's Telegram message."""

import uuid

import pytest

from app.api.v1 import feedback as api
from app.middleware import rate_limit
from app.services import feedback as svc
from tests.portfolio_fakes import U1, U2, make_client


class FakeStore:
    def __init__(self, monkeypatch):
        self.rows: dict[str, dict] = {}
        self.notified: list[tuple[dict, str | None]] = []
        self.missing = False
        monkeypatch.setattr(svc, "create", self.create)
        monkeypatch.setattr(svc, "list_mine", self.list_mine)
        monkeypatch.setattr(svc, "list_all", self.list_all)
        monkeypatch.setattr(svc, "update", self.update)
        monkeypatch.setattr(svc, "notify_owner", lambda r, u: self.notified.append((r, u)))

    def _check(self):
        if self.missing:
            raise RuntimeError('relation "feedback_reports" does not exist (42P01)')

    def create(self, user_id, row):
        self._check()
        rid = str(uuid.uuid4())
        self.rows[rid] = {**row, "id": rid, "user_id": user_id, "status": "open", "owner_note": None,
                          "created_at": f"2026-10-03T12:00:0{len(self.rows)}Z", "resolved_at": None}
        return dict(self.rows[rid])

    def list_mine(self, user_id, limit=50):
        self._check()
        return [r for r in self.rows.values() if r["user_id"] == user_id]

    def list_all(self, status=None, limit=100):
        self._check()
        return [r for r in self.rows.values() if status is None or r["status"] == status][:limit]

    def update(self, rid, data):
        self._check()
        if rid not in self.rows:
            return None
        self.rows[rid].update(data)
        return dict(self.rows[rid])


@pytest.fixture
def store(monkeypatch):
    return FakeStore(monkeypatch)


def _client(monkeypatch, level="free", uid=U1):
    return make_client(monkeypatch, api.router, level=level, uid=uid)


def test_send_report_stores_it_and_pings_the_owner(monkeypatch, store):
    body = {"kind": "bug", "message": "  Holdings total is wrong  ", "platform": "ios", "screen": "Holdings",
            "app_version": "0.3.2", "build": "57", "os_version": "iOS 26.0", "device_model": "iPhone17,1",
            "diagnostics": {"server_version": "1.0.7", "last_request": {"path": "/api/v1/holdings", "status": 500}}}
    r = _client(monkeypatch).post("/api/v1/feedback", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert set(out) == set(svc.PUBLIC_COLUMNS.split(", "))
    assert out["message"] == "Holdings total is wrong" and out["status"] == "open"
    saved = next(iter(store.rows.values()))
    assert saved["user_id"] == U1 and saved["diagnostics"]["server_version"] == "1.0.7"
    assert saved["device_model"] == "iPhone17,1"


def test_defaults_and_unknown_fields_ignored(monkeypatch, store):
    r = _client(monkeypatch).post("/api/v1/feedback", json={"message": "x", "password": "nope"})
    assert r.status_code == 201
    saved = next(iter(store.rows.values()))
    assert saved["kind"] == "bug" and saved["platform"] == "ios" and "password" not in saved


@pytest.mark.parametrize("body,code", [
    ({}, "invalid_message"),
    ({"message": "   "}, "invalid_message"),
    ({"message": "x" * 4001}, "invalid_message"),
    ({"message": "x", "kind": "rant"}, "invalid_kind"),
    ({"message": "x", "platform": "android"}, "invalid_platform"),
    ({"message": "x", "diagnostics": "text"}, "invalid_diagnostics"),
    ({"message": "x", "diagnostics": {"blob": "y" * 17_000}}, "invalid_diagnostics"),
    ({"message": "x", "screen": 5}, "invalid_screen"),
])
def test_validation(monkeypatch, store, body, code):
    r = _client(monkeypatch).post("/api/v1/feedback", json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code
    assert not store.rows


def test_long_short_fields_are_trimmed():
    row = svc.clean_report({"message": "x", "screen": "s" * 200, "app_version": "1" * 50})
    assert len(row["screen"]) == 80 and len(row["app_version"]) == 32


def test_my_reports_are_user_scoped(monkeypatch, store):
    _client(monkeypatch, uid=U1).post("/api/v1/feedback", json={"message": "mine"})
    _client(monkeypatch, uid=U2).post("/api/v1/feedback", json={"message": "theirs"})
    body = _client(monkeypatch, uid=U1).get("/api/v1/feedback/mine").json()
    assert body["count"] == 1 and body["items"][0]["message"] == "mine"


def test_owner_lists_and_triages(monkeypatch, store):
    _client(monkeypatch).post("/api/v1/feedback", json={"message": "bug 1"})
    rid = next(iter(store.rows))
    owner = _client(monkeypatch, level="owner")
    assert owner.get("/api/v1/feedback?status=open").json()["count"] == 1
    r = owner.patch(f"/api/v1/feedback/{rid}", json={"status": "fixed", "owner_note": " in 1.0.8 "})
    assert r.status_code == 200 and r.json()["status"] == "fixed"
    assert r.json()["owner_note"] == "in 1.0.8" and r.json()["resolved_at"]
    r = owner.patch(f"/api/v1/feedback/{rid}", json={"status": "open"})
    assert r.json()["resolved_at"] is None
    assert owner.get("/api/v1/feedback?status=fixed").json()["count"] == 0


def test_owner_routes_need_admin(monkeypatch, store):
    for level in ("free", "premium"):
        c = _client(monkeypatch, level=level)
        assert c.get("/api/v1/feedback").status_code == 403
        assert c.patch(f"/api/v1/feedback/{uuid.uuid4()}", json={"status": "fixed"}).status_code == 403


def test_owner_update_errors(monkeypatch, store):
    owner = _client(monkeypatch, level="owner")
    missing = owner.patch(f"/api/v1/feedback/{uuid.uuid4()}", json={"status": "fixed"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "report_not_found"
    assert owner.patch(f"/api/v1/feedback/{uuid.uuid4()}", json={"status": "done"}).json()["detail"]["code"] \
        == "invalid_status"
    assert owner.patch(f"/api/v1/feedback/{uuid.uuid4()}", json={}).json()["detail"]["code"] == "nothing_to_update"


def test_migration_missing_is_503(monkeypatch, store):
    store.missing = True
    r = _client(monkeypatch).post("/api/v1/feedback", json={"message": "x"})
    assert r.status_code == 503 and r.json()["detail"]["migration"] == svc.MIGRATION


def test_only_posting_a_report_is_strictly_rate_limited():
    assert rate_limit._get_tier("/api/v1/feedback", "POST")[0] == "strict"
    assert rate_limit._get_tier("/api/v1/feedback", "GET")[0] == "standard"


def test_owner_message_is_escaped_and_short():
    text = svc.owner_message({"kind": "bug", "message": "<b>x</b>" * 200, "platform": "ios",
                              "app_version": "0.3.2", "screen": "Holdings"}, "pedro")
    assert "&lt;b&gt;" in text and "ios · 0.3.2 · Holdings" in text and "pedro" in text
    assert text.endswith("…") and len(text) < 4096   # Telegram message limit
