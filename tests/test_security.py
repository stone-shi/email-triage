import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import security


def test_hash_and_verify_roundtrip():
    creds = security.hash_password("correct horse battery staple")
    assert security.verify_password(
        "correct horse battery staple",
        password_hash=creds["password_hash"],
        password_salt=creds["password_salt"],
        password_algo=creds["password_algo"],
        password_params=creds["password_params"],
    )


def test_verify_rejects_wrong_password():
    creds = security.hash_password("correct horse battery staple")
    assert not security.verify_password(
        "wrong password",
        password_hash=creds["password_hash"],
        password_salt=creds["password_salt"],
        password_algo=creds["password_algo"],
        password_params=creds["password_params"],
    )


def test_hash_is_salted():
    a = security.hash_password("same password")
    b = security.hash_password("same password")
    assert a["password_hash"] != b["password_hash"]
    assert a["password_salt"] != b["password_salt"]


def test_verify_rejects_unknown_algo():
    creds = security.hash_password("x")
    assert not security.verify_password(
        "x",
        password_hash=creds["password_hash"],
        password_salt=creds["password_salt"],
        password_algo="md5",
        password_params=creds["password_params"],
    )


def test_needs_rehash_false_for_current_params():
    creds = security.hash_password("x")
    assert not security.needs_rehash(creds["password_params"], creds["password_algo"])


def test_needs_rehash_true_for_weaker_params():
    weak = security.ScryptParams(n=4096)
    creds = security.hash_password("x", params=weak)
    assert security.needs_rehash(creds["password_params"], creds["password_algo"])


def test_needs_rehash_true_for_unknown_algo():
    assert security.needs_rehash("n=16384,r=8,p=1,dklen=32", "md5")


def test_session_token_hash_is_deterministic():
    token = security.new_session_token()
    assert security.hash_token(token) == security.hash_token(token)


def test_session_tokens_are_unique():
    assert security.new_session_token() != security.new_session_token()


def test_mcp_token_hash_roundtrip():
    token = security.new_mcp_token()
    assert security.hash_mcp_token(token) == security.hash_mcp_token(token)
    assert security.hash_mcp_token(token) != token


def test_validate_password_too_short():
    assert security.validate_password("short", min_length=10) is not None


def test_validate_password_same_as_current():
    assert security.validate_password("a_long_enough_password", current="a_long_enough_password") is not None


def test_validate_password_ok():
    assert security.validate_password("a_long_enough_password", min_length=10, current="old_password") is None
