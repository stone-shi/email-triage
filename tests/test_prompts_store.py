import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import prompts_store as ps


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    appdb.init_app_db(path)
    with appdb.get_conn(path) as c:
        yield c


def write_yaml(tmp_path, content: str) -> Path:
    path = tmp_path / "prompts.yml"
    path.write_text(content, encoding="utf-8")
    return path


class TestGetSetPrompt:
    def test_unset_key_returns_none(self, conn):
        assert ps.get_prompt(conn, "level_1_fast_triage") is None

    def test_set_then_get_roundtrip(self, conn):
        ps.set_prompt(conn, "level_1_fast_triage", "custom prompt text")
        assert ps.get_prompt(conn, "level_1_fast_triage") == "custom prompt text"

    def test_set_unknown_key_raises(self, conn):
        with pytest.raises(ValueError):
            ps.set_prompt(conn, "not_a_real_key", "x")

    def test_set_overwrites_existing_value(self, conn):
        ps.set_prompt(conn, "level_2_summarization", "first")
        ps.set_prompt(conn, "level_2_summarization", "second")
        assert ps.get_prompt(conn, "level_2_summarization") == "second"


class TestResetPrompt:
    def test_reset_removes_db_override(self, conn):
        ps.set_prompt(conn, "level_1_fast_triage", "custom")
        ps.reset_prompt(conn, "level_1_fast_triage")
        assert ps.get_prompt(conn, "level_1_fast_triage") is None

    def test_reset_of_unset_key_is_a_noop(self, conn):
        ps.reset_prompt(conn, "level_1_fast_triage")  # must not raise
        assert ps.get_prompt(conn, "level_1_fast_triage") is None


class TestLoadYamlPrompts:
    def test_missing_file_returns_empty(self, tmp_path):
        assert ps.load_yaml_prompts(tmp_path / "nope.yml") == {}

    def test_parses_system_key_per_prompt(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            "level_1_fast_triage:\n  system: \"custom yaml prompt\"\n"
            "level_2_summarization:\n  system: \"another one\"\n",
        )
        result = ps.load_yaml_prompts(yaml_path)
        assert result == {"level_1_fast_triage": "custom yaml prompt", "level_2_summarization": "another one"}

    def test_malformed_yaml_returns_empty_not_raises(self, tmp_path):
        yaml_path = tmp_path / "prompts.yml"
        yaml_path.write_text("not: valid: yaml: [", encoding="utf-8")
        assert ps.load_yaml_prompts(yaml_path) == {}


class TestGetAllPrompts:
    def test_falls_back_through_db_yaml_default(self, conn, tmp_path):
        yaml_path = write_yaml(tmp_path, "level_2_summarization:\n  system: \"yaml summary prompt\"\n")
        ps.set_prompt(conn, "level_1_fast_triage", "db override")

        result = ps.get_all_prompts(conn, yaml_path=yaml_path)

        assert result["level_1_fast_triage"]["value"] == "db override"
        assert result["level_1_fast_triage"]["source"] == "database"
        assert result["level_2_summarization"]["value"] == "yaml summary prompt"
        assert result["level_2_summarization"]["source"] == "prompts.yml"
        assert result["level_1_premium_escalation"]["value"] == ps.DEFAULT_PROMPTS["level_1_premium_escalation"]
        assert result["level_1_premium_escalation"]["source"] == "default"

    def test_includes_label_and_description(self, conn):
        result = ps.get_all_prompts(conn)
        assert result["level_1_fast_triage"]["label"]
        assert result["level_1_fast_triage"]["description"]

    def test_no_yaml_path_skips_yaml_layer(self, conn):
        result = ps.get_all_prompts(conn)
        assert result["level_2_summarization"]["source"] == "default"


class TestSeedFromYamlOrDefaults:
    def test_seeds_all_keys_when_db_empty(self, conn, tmp_path):
        yaml_path = write_yaml(tmp_path, "level_1_fast_triage:\n  system: \"yaml value\"\n")
        seeded = ps.seed_from_yaml_or_defaults(conn, yaml_path)
        assert seeded == len(ps.DEFAULT_PROMPTS)
        assert ps.get_prompt(conn, "level_1_fast_triage") == "yaml value"
        assert ps.get_prompt(conn, "level_2_summarization") == ps.DEFAULT_PROMPTS["level_2_summarization"]

    def test_never_overwrites_existing_admin_edit(self, conn, tmp_path):
        ps.set_prompt(conn, "level_1_fast_triage", "admin already edited this")
        yaml_path = write_yaml(tmp_path, "level_1_fast_triage:\n  system: \"yaml value\"\n")

        seeded = ps.seed_from_yaml_or_defaults(conn, yaml_path)

        assert ps.get_prompt(conn, "level_1_fast_triage") == "admin already edited this"
        assert seeded == len(ps.DEFAULT_PROMPTS) - 1

    def test_second_run_is_a_noop(self, conn, tmp_path):
        yaml_path = write_yaml(tmp_path, "level_1_fast_triage:\n  system: \"yaml value\"\n")
        ps.seed_from_yaml_or_defaults(conn, yaml_path)
        assert ps.seed_from_yaml_or_defaults(conn, yaml_path) == 0

    def test_seeds_without_yaml_path_uses_defaults(self, conn):
        seeded = ps.seed_from_yaml_or_defaults(conn)
        assert seeded == len(ps.DEFAULT_PROMPTS)
        for key, default_value in ps.DEFAULT_PROMPTS.items():
            assert ps.get_prompt(conn, key) == default_value


class TestGetAllPromptsRaw:
    def test_returns_only_db_overrides(self, conn):
        ps.set_prompt(conn, "level_1_fast_triage", "override")
        assert ps.get_all_prompts_raw(conn) == {"level_1_fast_triage": "override"}
