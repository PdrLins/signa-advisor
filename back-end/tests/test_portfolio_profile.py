"""Profile + notification prefs (migration 013): validation, tax-view
enforcement by level and country, defaults, migration-missing degradation."""

import pytest

from app.api.v1 import notifications, profile
from app.core import access
from app.services import notification_prefs, profile_service
from tests.portfolio_fakes import U1, FakePortfolioDB, make_client


@pytest.fixture
def db(monkeypatch):
    return FakePortfolioDB(monkeypatch)


def _client(monkeypatch, level="free"):
    return make_client(monkeypatch, profile.router, notifications.router, level=level)


# ---------------------------------------------------------------- catalog

def test_new_feature_keys_and_levels():
    cat = access.FEATURE_CATALOG
    for k in ("area.home", "area.insights", "area.coming_up", "area.profile", "action.accounts.edit",
              "action.transactions.edit", "action.import.csv"):
        assert cat[k][0] == "free", k
    assert cat["action.accounts.type"][0] == "free"      # free since migration 022
    assert cat["feature.tax_view"][0] == "premium"
    assert cat["area.brain"][0] == "owner"   # brain keys untouched


def test_migration_keys_match_catalog():
    """The level schema.sql leaves in access_features (later statements win,
    e.g. 022 re-levels keys first inserted by 013/014) equals the code default."""
    import pathlib
    import re
    db = pathlib.Path(__file__).parents[1] / "app/db"   # Advisor: base schema + every migration
    sql = "\n".join(f.read_text() for f in [db / "schema.sql", *sorted((db / "migrations").glob("0*.sql"))])
    rows = dict(re.findall(r"\('([a-z_.]+)', '(free|premium|owner)'", sql))
    assert rows and all(access.FEATURE_CATALOG[k][0] == lvl for k, lvl in rows.items() if k in access.FEATURE_CATALOG)


def test_schema_creates_every_table_the_code_uses():
    import pathlib
    import re
    root = pathlib.Path(__file__).parents[1]
    db = root / "app/db"   # Advisor: base schema + every migration
    sql = "\n".join(f.read_text() for f in [db / "schema.sql", *sorted((db / "migrations").glob("0*.sql"))])
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    used = set()
    for f in (root / "app").rglob("*.py"):
        used |= set(re.findall(r'\.table\(\s*"(\w+)"', f.read_text()))
    used.discard("app_logs")   # optional log persistence, tolerated when missing
    assert used and used <= created, f"tables missing from schema.sql: {sorted(used - created)}"


# ---------------------------------------------------------------- migration missing

def test_profile_503_before_migration(monkeypatch, db):
    db.missing = True
    c = _client(monkeypatch)
    for r in (c.get("/api/v1/profile"), c.put("/api/v1/profile", json={"country": "CA"}),
              c.get("/api/v1/notifications/prefs")):
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "migration_required"


# ---------------------------------------------------------------- profile

def test_profile_defaults(monkeypatch, db):
    body = _client(monkeypatch).get("/api/v1/profile").json()
    assert body["home_currency"] == "CAD" and body["country"] is None
    assert body["dividend_tax_view"] == "before" and body["compare_index"] is None
    assert body["holdings_native_currency"] is False and body["access_level"] == "free"
    assert body["email"] is None and "slots" in body


def test_profile_update_and_validation(monkeypatch, db):
    c = _client(monkeypatch)
    r = c.put("/api/v1/profile", json={"display_name": "  Pedro   L ", "country": "br", "home_currency": "usd",
                                       "compare_index": "xeqt.to", "holdings_native_currency": True,
                                       "language": "pt"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["display_name"] == "Pedro L" and b["country"] == "BR" and b["home_currency"] == "USD"
    assert b["compare_index"] == "XEQT.TO" and b["holdings_native_currency"] is True and b["language"] == "pt"
    assert db.settings[U1]["country"] == "BR"
    # null clears
    assert c.put("/api/v1/profile", json={"compare_index": None}).json()["compare_index"] is None


@pytest.mark.parametrize("patch,code", [
    ({"country": "XX"}, "invalid_country"),
    ({"country": "CAN"}, "invalid_country"),
    ({"home_currency": "ABC"}, "invalid_currency"),
    ({"home_currency": None}, "invalid_currency"),
    ({"language": "fr"}, "invalid_language"),
    ({"compare_index": "AAPL"}, "invalid_compare_index"),
    ({"display_name": "x" * 61}, "invalid_display_name"),
    ({"holdings_native_currency": "yes"}, "invalid_value"),
    ({"dividend_tax_view": "sideways"}, "invalid_tax_view"),
])
def test_profile_validation_errors(monkeypatch, db, patch, code):
    r = _client(monkeypatch).put("/api/v1/profile", json=patch)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_unknown_profile_field_rejected(monkeypatch, db):
    assert _client(monkeypatch).put("/api/v1/profile", json={"access_level": "owner"}).status_code == 422


# ---------------------------------------------------------------- tax view

@pytest.mark.parametrize("level,country,status,code", [
    ("free", "CA", 403, "upgrade_required"),
    ("free", None, 403, "upgrade_required"),
    ("premium", "BR", 422, "tax_view_unavailable"),
    ("premium", None, 422, "tax_view_unavailable"),
    ("premium", "CA", 200, None),
    ("premium", "US", 200, None),
    ("owner", "US", 200, None),
])
def test_tax_view_set_rules(monkeypatch, db, level, country, status, code):
    if country:
        db.settings[U1] = {"user_id": U1, "country": country}
    r = _client(monkeypatch, level).put("/api/v1/profile", json={"dividend_tax_view": "after"})
    assert r.status_code == status, r.text
    if code:
        assert r.json()["detail"]["code"] == code
    else:
        assert r.json()["dividend_tax_view"] == "after"


def test_tax_view_set_with_country_in_same_request(monkeypatch, db):
    r = _client(monkeypatch, "premium").put("/api/v1/profile", json={"country": "CA", "dividend_tax_view": "after"})
    assert r.status_code == 200 and r.json()["dividend_tax_view"] == "after"


@pytest.mark.parametrize("level,country,effective", [
    ("premium", "CA", "after"), ("free", "CA", "before"), ("premium", "FR", "before"), ("premium", None, "before"),
])
def test_tax_view_read_is_effective(monkeypatch, db, level, country, effective):
    db.settings[U1] = {"user_id": U1, "country": country, "dividend_tax_view": "after"}
    b = _client(monkeypatch, level).get("/api/v1/profile").json()
    assert b["dividend_tax_view"] == effective and b["dividend_tax_view_stored"] == "after"
    assert b["tax_view_available"] is (effective == "after")


def test_setting_before_is_always_allowed(monkeypatch, db):
    assert _client(monkeypatch).put("/api/v1/profile", json={"dividend_tax_view": "before"}).status_code == 200


def test_effective_tax_view_pure():
    assert profile_service.effective_tax_view("after", "premium", "us") == "after"
    assert profile_service.effective_tax_view("before", "owner", "CA") == "before"


# ---------------------------------------------------------------- notification prefs

def test_prefs_defaults_without_row(monkeypatch, db):
    b = _client(monkeypatch).get("/api/v1/notifications/prefs").json()
    assert b["is_default"] is True
    p = b["prefs"]
    assert set(p) == {"exdiv_reminder", "dividend_paid", "dividend_change", "check_changed", "earnings",
                      "big_move", "analyst_ratings", "economy"}
    assert p["big_move"] == {"enabled": True, "threshold_pct": 5.0}
    assert p["analyst_ratings"]["enabled"] is False and p["economy"]["enabled"] is True


def test_prefs_partial_update_merges(monkeypatch, db):
    c = _client(monkeypatch)
    r = c.put("/api/v1/notifications/prefs", json={"big_move": {"threshold_pct": 8}, "economy": {"enabled": False}})
    assert r.status_code == 200, r.text
    p = c.get("/api/v1/notifications/prefs").json()
    assert p["is_default"] is False
    assert p["prefs"]["big_move"] == {"enabled": True, "threshold_pct": 8.0}
    assert p["prefs"]["economy"]["enabled"] is False and p["prefs"]["earnings"]["enabled"] is True


@pytest.mark.parametrize("patch,status", [
    ({"nope": {"enabled": True}}, 422),
    ({"earnings": {"enabled": "yes"}}, 422),
    ({"earnings": {"threshold_pct": 5}}, 422),
    ({"big_move": {"threshold_pct": 0.5}}, 422),
    ({"big_move": {"threshold_pct": 99}}, 422),
    ({"earnings": True}, 422),
    ({}, 400),
])
def test_prefs_validation(monkeypatch, db, patch, status):
    assert _client(monkeypatch).put("/api/v1/notifications/prefs", json=patch).status_code == status


def test_prefs_merge_ignores_junk():
    p = notification_prefs.merge_with_defaults({"big_move": {"threshold_pct": 1000}, "junk": 1,
                                                "earnings": {"enabled": False}})
    assert p["big_move"]["threshold_pct"] == 5.0 and p["earnings"]["enabled"] is False and "junk" not in p
