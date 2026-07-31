import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import secretstore


@pytest.fixture(autouse=True)
def _reset_cache():
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()
    os.environ.pop("EMAIL_TRIAGE_SECRET_KEY", None)


def test_generates_key_file_on_first_use(tmp_path):
    secretstore.load_key(tmp_path)
    key_file = secretstore.key_path(tmp_path)
    assert key_file.exists()
    assert oct(key_file.stat().st_mode)[-3:] == "600"


def test_encrypt_decrypt_roundtrip(tmp_path):
    blob = secretstore.encrypt({"password": "hunter2"}, tmp_path)
    assert secretstore.decrypt(blob, tmp_path) == {"password": "hunter2"}


def test_decrypt_empty_is_empty_dict(tmp_path):
    assert secretstore.decrypt(None, tmp_path) == {}
    assert secretstore.decrypt("", tmp_path) == {}


def test_decrypt_tampered_blob_raises(tmp_path):
    import json

    blob = secretstore.encrypt({"password": "hunter2"}, tmp_path)
    envelope = json.loads(blob)
    envelope["ct"] = envelope["ct"][:-4] + ("A" * 4 if envelope["ct"][-4:] != "AAAA" else "BBBB")
    with pytest.raises(secretstore.SecretDecryptError):
        secretstore.decrypt(json.dumps(envelope), tmp_path)


def test_decrypt_garbage_raises(tmp_path):
    secretstore.load_key(tmp_path)
    with pytest.raises(secretstore.SecretDecryptError):
        secretstore.decrypt("not json", tmp_path)


def test_env_key_used_when_no_file(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", Fernet.generate_key().decode())
    key_id, key = secretstore.load_key(tmp_path)
    assert not secretstore.key_path(tmp_path).exists()


def test_conflicting_env_and_file_key_raises(tmp_path, monkeypatch):
    secretstore.load_key(tmp_path)  # generates a file key
    secretstore.reset_key_cache()
    monkeypatch.setenv("EMAIL_TRIAGE_SECRET_KEY", "a-completely-different-material")
    with pytest.raises(secretstore.SecretKeyError):
        secretstore.load_key(tmp_path)


def test_rejects_world_readable_key_file(tmp_path):
    secretstore.load_key(tmp_path)
    secretstore.reset_key_cache()
    secretstore.key_path(tmp_path).chmod(0o644)
    with pytest.raises(secretstore.SecretKeyError):
        secretstore.load_key(tmp_path)
