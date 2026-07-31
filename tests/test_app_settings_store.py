import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent.parent))

import app_settings_store as ass
import appdb
import secretstore
import users_store as us
from app_errors import ValidationError


@pytest.fixture(autouse=True)
def _isolated_secret_key(monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


@pytest.fixture
def user(conn):
    return us.create_user(conn, username="bob", password="a_long_enough_password")


def test_set_and_get_plain_value(conn):
    ass.set_value(conn, "triage_model", "gpt-nano")
    assert ass.get_value(conn, "triage_model") == "gpt-nano"


def test_set_and_get_bool_value(conn):
    ass.set_value(conn, "scheduler.enabled", False)
    assert ass.get_value(conn, "scheduler.enabled") is False


def test_set_and_get_json_value(conn):
    ass.set_value(conn, "triage.blacklist_keywords", ["spam", "promo"])
    assert ass.get_value(conn, "triage.blacklist_keywords") == ["spam", "promo"]


def test_secret_value_is_encrypted_at_rest(conn):
    ass.set_value(conn, "triage_api_key", "sk-super-secret")
    raw = ass.get_raw(conn, "triage_api_key")
    assert "sk-super-secret" not in raw["value"]
    assert ass.get_value(conn, "triage_api_key") == "sk-super-secret"


def test_get_value_unset_key_is_none(conn):
    assert ass.get_value(conn, "triage_model") is None


def test_set_value_rejects_unknown_key(conn):
    with pytest.raises(ValidationError):
        ass.set_value(conn, "not_a_real_key", "x")


def test_set_many_rejects_any_unknown_key_atomically(conn):
    with pytest.raises(ValidationError):
        ass.set_many(conn, {"triage_model": "gpt-nano", "bogus": "x"})
    assert ass.get_value(conn, "triage_model") is None


def test_get_all_for_api_masks_secrets(conn):
    ass.set_value(conn, "triage_api_key", "sk-super-secret")
    ass.set_value(conn, "triage_model", "gpt-nano")
    out = ass.get_all_for_api(conn)
    assert out["triage_api_key"]["value"] == "••••"
    assert out["triage_api_key"]["set"] is True
    assert out["triage_model"]["value"] == "gpt-nano"


def test_user_override_requires_user_overridable_key(conn, user):
    with pytest.raises(ValidationError):
        ass.set_user_value(conn, user["id"], "triage_model", "gpt-nano")


def test_user_override_roundtrip(conn, user):
    ass.set_user_value(conn, user["id"], "triage.whitelist_vip_senders", ["boss@example.com"])
    assert ass.get_user_value(conn, user["id"], "triage.whitelist_vip_senders") == ["boss@example.com"]


def test_apply_to_settings_overlays_global_then_user(conn, user):
    settings = SimpleNamespace(
        triage_model="default-model",
        triage=SimpleNamespace(confidence_threshold=0.8, whitelist_vip_senders=[]),
    )
    ass.set_value(conn, "triage_model", "global-model")
    ass.set_user_value(conn, user["id"], "triage.whitelist_vip_senders", ["boss@example.com"])
    ass.apply_to_settings(conn, settings, user_id=user["id"])
    assert settings.triage_model == "global-model"
    assert settings.triage.whitelist_vip_senders == ["boss@example.com"]


def test_apply_to_settings_without_user_id_skips_user_overrides(conn, user):
    settings = SimpleNamespace(triage=SimpleNamespace(whitelist_vip_senders=["default"]))
    ass.set_user_value(conn, user["id"], "triage.whitelist_vip_senders", ["boss@example.com"])
    ass.apply_to_settings(conn, settings, user_id=None)
    assert settings.triage.whitelist_vip_senders == ["default"]
