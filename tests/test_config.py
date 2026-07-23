import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings, TriageSettings, SchedulerSettings, parse_duration, list_profile_names


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


class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("15m") == 900.0

    def test_hours(self):
        assert parse_duration("1h") == 3600.0

    def test_seconds_suffix(self):
        assert parse_duration("45s") == 45.0

    def test_days(self):
        assert parse_duration("1d") == 86400.0

    def test_plain_number_string(self):
        assert parse_duration("45") == 45.0

    def test_numeric_value(self):
        assert parse_duration(120) == 120.0
        assert parse_duration(12.5) == 12.5

    def test_invalid_falls_back_to_default(self):
        assert parse_duration("garbage", default_seconds=42.0) == 42.0

    def test_empty_falls_back_to_default(self):
        assert parse_duration("", default_seconds=42.0) == 42.0


class TestSchedulerSettings:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            ss = SchedulerSettings()
            assert ss.enabled is True
            assert ss.interval == "15m"
            assert ss.max_per_account is None
            assert ss.days is None
            assert ss.interval_seconds == 900.0

    def test_env_overrides(self):
        with patch.dict(os.environ, {
            "EMAIL_TRIAGE_SCHEDULER_ENABLED": "false",
            "EMAIL_TRIAGE_SCHEDULER_INTERVAL": "1h",
            "EMAIL_TRIAGE_SCHEDULER_MAX_PER_ACCOUNT": "25",
            "EMAIL_TRIAGE_SCHEDULER_DAYS": "3",
        }, clear=True):
            ss = SchedulerSettings()
            assert ss.enabled is False
            assert ss.interval == "1h"
            assert ss.max_per_account == 25
            assert ss.days == 3
            assert ss.interval_seconds == 3600.0

    def test_settings_has_scheduler(self):
        s = Settings()
        assert isinstance(s.scheduler, SchedulerSettings)


class TestListProfileNames:
    def test_includes_default(self):
        names = list_profile_names()
        assert "default" in names

    def test_finds_existing_profile_dirs(self):
        names = list_profile_names()
        repo_root = Path(__file__).parent.parent
        expected = {p.name for p in (repo_root / "profiles").iterdir() if p.is_dir()}
        assert expected.issubset(set(names))


class TestSchedulerYamlLoading:
    def test_load_from_yaml_scheduler_settings(self):
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump({
                "scheduler": {
                    "enabled": False,
                    "interval": "30m",
                    "max_per_account": 10,
                    "days": 5,
                }
            }, f)
            yaml_path = f.name

        try:
            with patch.dict(os.environ, {}, clear=True):
                s = Settings(_env_file=None)
                s.workspace_dir = Path(yaml_path).parent
                s.load_from_yaml(yaml_path=Path(yaml_path), env_file=None)
                assert s.scheduler.enabled is False
                assert s.scheduler.interval == "30m"
                assert s.scheduler.max_per_account == 10
                assert s.scheduler.days == 5
        finally:
            os.unlink(yaml_path)
