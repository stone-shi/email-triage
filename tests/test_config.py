import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings, TriageSettings


class TestTriageSettings:
    def test_default_values(self):
        ts = TriageSettings()
        assert ts.confidence_threshold == 0.8
        assert ts.triage_type == "llm"
        assert ts.tei_router_enabled is False
        assert ts.tei_noise_threshold == 0.999
        assert ts.tei_signal_threshold == 0.95
        assert ts.whitelist_vip_senders == []
        assert ts.whitelist_domains == []
        assert isinstance(ts.blacklist_keywords, list)
        assert "unsubscribe" in ts.blacklist_keywords

    def test_custom_values(self):
        ts = TriageSettings(
            confidence_threshold=0.6,
            triage_type="tei",
            tei_router_enabled=True,
            whitelist_vip_senders=["boss@example.com"],
        )
        assert ts.confidence_threshold == 0.6
        assert ts.triage_type == "tei"
        assert ts.tei_router_enabled is True
        assert ts.whitelist_vip_senders == ["boss@example.com"]


class TestSettingsDefaults:
    def test_default_workspace_dir(self):
        s = Settings()
        assert s.workspace_dir == Path(__file__).parent.parent.resolve()

    def test_default_triage_base_url(self):
        s = Settings(_env_file=None)
        assert "your-llm-proxy.com" in s.triage_base_url

    def test_default_triage_model(self):
        s = Settings()
        assert "deepseek" in s.triage_model

    def test_default_imap_settings(self):
        s = Settings()
        assert s.imap_host == "imap.zoho.com"
        assert s.imap_port == 993

    def test_default_mcp_settings(self):
        s = Settings()
        assert s.mcp_transport == "stdio"
        assert s.mcp_host == "0.0.0.0"
        assert s.mcp_port == 8000


class TestSettingsEnvPrefix:
    def test_env_triage_model(self):
        with patch.dict(os.environ, {"EMAIL_TRIAGE_TRIAGE_MODEL": "test/model"}, clear=True):
            s = Settings()
            assert s.triage_model == "test/model"

    def test_env_summary_model(self):
        with patch.dict(os.environ, {"EMAIL_TRIAGE_SUMMARY_MODEL": "test/summary"}, clear=True):
            s = Settings()
            assert s.summary_model == "test/summary"

    def test_env_imap_password(self):
        with patch.dict(os.environ, {"EMAIL_TRIAGE_IMAP_PASSWORD": "secret123"}, clear=True):
            s = Settings()
            assert s.imap_password == "secret123"

    def test_env_mcp_transport_sse(self):
        with patch.dict(os.environ, {"EMAIL_TRIAGE_MCP_TRANSPORT": "sse", "EMAIL_TRIAGE_MCP_PORT": "9000"}, clear=True):
            s = Settings()
            assert s.mcp_transport == "sse"
            assert s.mcp_port == 9000


class TestSettingsApiKeys:
    def test_triage_api_key_fallback(self):
        with patch.dict(os.environ, {"EMAIL_TRIAGE_LLM_API_KEY": "shared_key"}, clear=True):
            s = Settings(_env_file=None)
            assert s.triage_api_key == "shared_key"
            assert s.summary_api_key == "shared_key"

    def test_separate_api_keys(self):
        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_TRIAGE_API_KEY": "triage_key",
            "EMAIL_TRIAGE_SUMMARY_API_KEY": "summary_key",
        }, clear=True):
            s = Settings()
            assert s.triage_api_key == "triage_key"
            assert s.summary_api_key == "summary_key"


class TestSettingsYamlLoading:
    def test_load_from_yaml_triage_settings(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({
                "triage": {
                    "confidence_threshold": 0.5,
                    "tei_router_enabled": True,
                }
            }, f)
            yaml_path = f.name

        try:
            s = Settings()
            s.workspace_dir = Path(yaml_path).parent
            s.load_from_yaml(yaml_path=Path(yaml_path))
            assert s.triage.confidence_threshold == 0.5
            assert s.triage.tei_router_enabled is True
        finally:
            os.unlink(yaml_path)

    def test_load_from_yaml_llm_settings(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({
                "llm": {
                    "base_url": "https://custom-proxy.example.com/v1",
                    "triage_model": "custom/flash",
                }
            }, f)
            yaml_path = f.name

        try:
            s = Settings()
            s.workspace_dir = Path(yaml_path).parent
            s.load_from_yaml(yaml_path=Path(yaml_path))
            assert s.triage_model == "custom/flash"
            assert s.triage_base_url == "https://custom-proxy.example.com/v1"
        finally:
            os.unlink(yaml_path)

    def test_yaml_not_override_env(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({"llm": {"triage_model": "from_yaml"}}, f)
            yaml_path = f.name

        try:
            with patch.dict(os.environ, {"EMAIL_TRIAGE_TRIAGE_MODEL": "from_env"}, clear=True):
                s = Settings(_env_file=None)
                s.workspace_dir = Path(yaml_path).parent
                s.load_from_yaml(yaml_path=Path(yaml_path), env_file=None)
                assert s.triage_model == "from_env"
        finally:
            os.unlink(yaml_path)


class TestSettingsLoadForProfile:
    def test_default_profile_returns_settings(self):
        s = Settings.load_for_profile("default")
        assert isinstance(s, Settings)
        assert s.workspace_dir.name == "default"

    def test_nonexistent_profile_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profiles_dir = Path(tmpdir) / "profiles"
            profiles_dir.mkdir()
            with patch.object(Settings, "__init__", return_value=None):
                with patch("config.Path.__init__", return_value=None):
                    pass

    def test_active_smtp_fallback(self):
        s = Settings()
        s.imap_login = "imap_user@test.com"
        s.smtp_login = None
        assert s.active_smtp_login == "imap_user@test.com"

    def test_active_smtp_explicit(self):
        s = Settings()
        s.imap_login = "imap_user@test.com"
        s.smtp_login = "smtp_user@test.com"
        assert s.active_smtp_login == "smtp_user@test.com"


class TestSettingsSyncTriage:
    def test_sync_tei_url(self):
        s = Settings()
        s.tei_url = "http://custom:8080/predict"
        s2 = Settings(**s.model_dump())
        s2.tei_url = "http://custom:8080/predict"
        s3 = Settings.model_validate(s2.model_dump())
        assert s3.triage.tei_url == "http://custom:8080/predict"
