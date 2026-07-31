import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import mcp_server
import users_store as us


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    appdb.init_app_db(db_path)
    yield db_path


@pytest.fixture
def admin(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(
            conn, username="admin", password="a_long_enough_password", is_admin=True, must_change_password=False
        )


@pytest.fixture
def non_admin(app_db):
    with appdb.get_conn(app_db) as conn:
        return us.create_user(conn, username="bob", password="a_long_enough_password", must_change_password=False)


@pytest.fixture
def client(app_db):
    from starlette.testclient import TestClient

    return TestClient(mcp_server.mcp.sse_app())


def login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _insert_run(app_db, *, user_id, account, window_end, sample_size=10, f1=0.8, quality_avg=7.5, status="ok"):
    with appdb.get_conn(app_db) as conn:
        conn.execute(
            """INSERT INTO quality_check_runs
               (user_id, account, window_start, window_end, sample_rate, population_size, sample_size,
                judge_model, level_precision, level_recall, level_f1, summary_quality_avg,
                summary_quality_count, started_at, finished_at, status, created_at)
               VALUES (?, ?, ?, ?, 0.1, ?, ?, 'judge/model', 0.8, 0.8, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, account, window_end, window_end, sample_size * 10, sample_size, f1,
                quality_avg, sample_size, window_end, window_end, status, window_end,
            ),
        )


class TestQualityTrend:
    def test_requires_login(self, client, app_db):
        assert client.get("/api/quality/trend").status_code == 401

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        assert client.get("/api/quality/trend").status_code == 403

    def test_zero_filled_when_no_runs(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=7")
        assert resp.status_code == 200
        days = resp.json()["days"]
        assert len(days) == 7
        assert all(d["sample_size"] == 0 for d in days)
        assert all(d["f1"] is None for d in days)

    def test_weighted_average_across_accounts(self, client, admin, app_db):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).isoformat()
        _insert_run(app_db, user_id=admin["id"], account="a@x.com", window_end=today, sample_size=10, f1=1.0)
        _insert_run(app_db, user_id=admin["id"], account="b@x.com", window_end=today, sample_size=30, f1=0.0)

        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=1")
        assert resp.status_code == 200
        day = resp.json()["days"][0]
        # weighted by sample_size: (10*1.0 + 30*0.0) / 40 = 0.25
        assert day["f1"] == pytest.approx(0.25)
        assert day["sample_size"] == 40

    def test_error_status_runs_excluded_from_metrics(self, client, admin, app_db):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).isoformat()
        _insert_run(app_db, user_id=admin["id"], account="a@x.com", window_end=today, status="error")

        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=1")
        day = resp.json()["days"][0]
        assert day["sample_size"] == 0
        assert day["f1"] is None

    def test_run_count_and_error_count_reported(self, client, admin, app_db):
        # A day with a failed run and a successful run must still be distinguishable
        # from a day with no runs at all -- both would otherwise look identical
        # (blank metrics) if only run_count/error_count weren't reported.
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).isoformat()
        _insert_run(app_db, user_id=admin["id"], account="a@x.com", window_end=today, status="error")
        _insert_run(app_db, user_id=admin["id"], account="b@x.com", window_end=today, status="ok")

        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=1")
        day = resp.json()["days"][0]
        assert day["run_count"] == 2
        assert day["error_count"] == 1
        assert day["no_data_count"] == 0

    def test_no_data_count_reported(self, client, admin, app_db):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).isoformat()
        _insert_run(app_db, user_id=admin["id"], account="a@x.com", window_end=today, status="no_data")

        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=1")
        day = resp.json()["days"][0]
        assert day["run_count"] == 1
        assert day["no_data_count"] == 1
        assert day["error_count"] == 0

    def test_zero_filled_days_report_zero_run_count(self, client, admin):
        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/trend?days=7")
        assert all(d["run_count"] == 0 for d in resp.json()["days"])


class TestQualityRuns:
    def test_requires_login(self, client, app_db):
        assert client.get("/api/quality/runs").status_code == 401

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        assert client.get("/api/quality/runs").status_code == 403

    def test_lists_recent_runs_with_username(self, client, admin, app_db):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).isoformat()
        _insert_run(app_db, user_id=admin["id"], account="a@x.com", window_end=today)

        login(client, "admin", "a_long_enough_password")
        resp = client.get("/api/quality/runs?days=7")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["username"] == "admin"
        assert runs[0]["account"] == "a@x.com"


class TestQualityRunNow:
    def test_requires_login(self, client, app_db):
        assert client.post("/api/quality/run-now").status_code == 401

    def test_non_admin_forbidden(self, client, non_admin):
        login(client, "bob", "a_long_enough_password")
        assert client.post("/api/quality/run-now").status_code == 403

    def test_admin_can_trigger(self, client, admin, monkeypatch):
        import quality_check

        monkeypatch.setattr(quality_check, "run_quality_check_all_profiles", lambda *a, **kw: {"accounts": []})
        login(client, "admin", "a_long_enough_password")
        resp = client.post("/api/quality/run-now")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
