import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import secretstore


@pytest.fixture(autouse=True)
def _isolate_secret_store(tmp_path, monkeypatch):
    """Every test that touches secretstore (directly, or indirectly via
    integrations_store/app_settings_store) must never read or compare against
    the real repo's data/secret.key. Once a real deployment has generated one,
    any test that only fakes EMAIL_TRIAGE_SECRET_KEY (without also redirecting
    the data directory) would otherwise collide with it and hit secretstore's
    key-mismatch guard.
    """
    monkeypatch.setattr(secretstore, "DEFAULT_DATA_DIR", tmp_path)
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture(autouse=True)
def _isolate_app_db(tmp_path, monkeypatch):
    """Default every test to a fresh, empty data/app.db path, so any code path
    that does `if appdb.DEFAULT_APP_DB_PATH.exists(): ...` (config.py's
    load_for_user, triage.py's prompt overlay, mcp_server.py's DB-vs-legacy
    dual paths, etc.) never sees the real deployment's database -- which now
    genuinely exists on disk and would otherwise make test behavior depend on
    whatever an admin has actually configured/edited live. Test files that
    need a real (but still isolated) app.db already point this at their own
    tmp_path explicitly, which simply overrides this default within that test.
    """
    monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", tmp_path / "unused-app.db")
