import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

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
