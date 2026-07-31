import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import appdb
import quality_check as qc
from db import EmailDB


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.workspace_dir = Path(__file__).parent.parent
    s.triage_base_url = "https://triage-proxy.example.com/v1"
    s.triage_api_key = "triage-key"
    s.triage_model = "deepseek/triage-flash"
    s.summary_base_url = "https://summary-proxy.example.com/v1"
    s.summary_api_key = "summary-key"
    s.summary_model = "deepseek/summary-pro"

    triage_config = MagicMock()
    triage_config.confidence_threshold = 0.8
    triage_config.triage_type = "tei"  # deliberately non-"llm" -- _build_judge_engine must override this
    triage_config.whitelist_vip_senders = []
    triage_config.whitelist_domains = []
    triage_config.blacklist_keywords = []
    triage_config.blacklist_senders = []
    s.triage = triage_config

    qcs = MagicMock()
    qcs.judge_base_url = "https://judge-proxy.example.com/v1"
    qcs.judge_model = "openai/gpt-judge"
    qcs.judge_api_key = "judge-key"
    qcs.sample_rate = 0.5
    s.quality_check = qcs
    return s


class TestBuildJudgeEngine:
    def test_returns_none_when_unconfigured(self, mock_settings):
        mock_settings.quality_check.judge_base_url = ""
        assert qc._build_judge_engine(mock_settings) is None

    def test_returns_none_when_model_missing(self, mock_settings):
        mock_settings.quality_check.judge_model = ""
        assert qc._build_judge_engine(mock_settings) is None

    def test_engine_uses_judge_endpoint_and_model(self, mock_settings):
        engine = qc._build_judge_engine(mock_settings)
        assert engine is not None
        assert engine.settings.triage_base_url == "https://judge-proxy.example.com/v1"
        assert engine.settings.summary_base_url == "https://judge-proxy.example.com/v1"
        assert engine.settings.triage_model == "openai/gpt-judge"
        assert engine.settings.summary_model == "openai/gpt-judge"
        assert engine.triage_headers["Authorization"] == "Bearer judge-key"

    def test_forces_llm_triage_type_even_if_account_uses_tei(self, mock_settings):
        engine = qc._build_judge_engine(mock_settings)
        assert engine.settings.triage.triage_type == "llm"
        # The original (non-judge) settings object must be untouched.
        assert mock_settings.triage.triage_type == "tei"

    def test_judge_calls_never_touch_the_real_account_db(self, mock_settings):
        engine = qc._build_judge_engine(mock_settings)
        engine.db.log_token_usage("level_1_classification", "openai/gpt-judge", 123)
        assert engine._quality_check_token_sink.tokens_used == 123


class TestMacroPRF1:
    def test_perfect_agreement(self):
        pairs = [(0, 0), (1, 1), (2, 2), (0, 0)]
        result = qc._macro_prf1(pairs)
        assert result == {"precision": 1.0, "recall": 1.0, "f1": 1.0}

    def test_complete_disagreement(self):
        pairs = [(0, 1), (1, 2), (2, 0)]
        result = qc._macro_prf1(pairs)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["f1"] == 0.0

    def test_empty_pairs(self):
        result = qc._macro_prf1([])
        assert result == {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    def test_partial_agreement(self):
        # Judge (ground truth) says [0, 1, 2, 2]; production (prediction) says [0, 1, 1, 2].
        pairs = [(0, 0), (1, 1), (1, 2), (2, 2)]
        result = qc._macro_prf1(pairs)
        assert 0.0 < result["f1"] < 1.0


def _fake_population(counts_by_level):
    """Builds a fake population list: {level: count} -> list of cached-row dicts."""
    rows = []
    i = 0
    for level, count in counts_by_level.items():
        for _ in range(count):
            rows.append({"message_id": f"m{i}", "triage_level": level, "account": "acct"})
            i += 1
    return rows


class TestStratifiedSample:
    def test_matches_the_reported_example(self):
        # 20 level-0, 50 level-1, 30 level-2 -- 10% should yield 2/5/3.
        population = _fake_population({0: 20, 1: 50, 2: 30})
        sample = qc._stratified_sample(population, 0.1)
        counts = {0: 0, 1: 0, 2: 0}
        for row in sample:
            counts[row["triage_level"]] += 1
        assert counts == {0: 2, 1: 5, 2: 3}

    def test_floors_at_one_per_present_level(self):
        # A tiny level-0 group (2 messages) at 1% would round to 0 without the floor.
        population = _fake_population({0: 2, 1: 200})
        sample = qc._stratified_sample(population, 0.01)
        counts = {0: 0, 1: 0}
        for row in sample:
            counts[row["triage_level"]] += 1
        assert counts[0] >= 1
        assert counts[1] >= 1

    def test_never_samples_a_level_with_zero_population(self):
        population = _fake_population({1: 10})
        sample = qc._stratified_sample(population, 0.5)
        assert all(row["triage_level"] == 1 for row in sample)

    def test_rate_of_1_returns_everything(self):
        population = _fake_population({0: 3, 1: 3, 2: 3})
        sample = qc._stratified_sample(population, 1.0)
        assert len(sample) == 9

    def test_empty_population(self):
        assert qc._stratified_sample([], 0.1) == []


class TestRunQualityCheckForUser:
    @pytest.fixture
    def app_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "app.db"
        monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
        appdb.init_app_db(db_path)
        with appdb.get_conn(db_path) as conn:
            conn.execute(
                """INSERT INTO users (id, username, workspace_slug, password_hash, password_salt,
                                       created_at, updated_at)
                   VALUES (1, 'alice', 'alice', 'x', 'y', '2020-01-01', '2020-01-01')"""
            )
        return db_path

    def _account_client(self, account):
        ac = MagicMock()
        ac.account = account
        ac.provider = "gmail"
        ac.client = MagicMock()
        return ac

    def _seed(self, tmp_path, account, level, n):
        db = EmailDB(db_path=tmp_path / "email_cache.db")
        for i in range(n):
            mid = f"{account}-{level}-{i}"
            db.upsert_email_metadata(message_id=mid, account=account, sender="a@b.com", subject="s", snippet="sn")
            db.save_triage_result(
                message_id=mid, account=account, sender="a@b.com", subject="s",
                date_str="2024-01-01", level_0_status="passed", triage_level=level, tag="t",
            )

    def test_pools_across_accounts_before_sampling(self, app_db, mock_settings, tmp_path):
        from datetime import datetime, timedelta, timezone

        mock_settings.workspace_dir = tmp_path
        mock_settings.quality_check.sample_rate = 0.1
        # Account A has all the level-0 traffic, account B has all the level-1 traffic.
        # A flat 10% draw per account could easily miss level 0 or level 1 entirely;
        # pooled+stratified sampling must still guarantee at least one of each.
        self._seed(tmp_path, "a@x.com", 0, 20)
        self._seed(tmp_path, "b@x.com", 1, 50)
        accounts = [self._account_client("a@x.com"), self._account_client("b@x.com")]

        window_end = datetime.now(timezone.utc)
        window_start = window_end - timedelta(hours=24)

        fake_judge_engine = MagicMock()
        fake_judge_engine.run_level_1_classification.return_value = (0, "r", 0.95, "low", {})

        with patch.object(qc, "_build_judge_engine", return_value=fake_judge_engine):
            with appdb.get_conn(app_db) as conn:
                results = qc.run_quality_check_for_user(
                    conn, user_id=1, accounts=accounts, profile_settings=mock_settings,
                    window_start=window_start, window_end=window_end,
                )

        by_account = dict(zip([a.account for a in accounts], results))
        assert by_account["a@x.com"]["population_size"] == 20
        assert by_account["a@x.com"]["sample_size"] == 2  # 20 * 10%
        assert by_account["b@x.com"]["population_size"] == 50
        assert by_account["b@x.com"]["sample_size"] == 5  # 50 * 10%

    def test_no_accounts_returns_empty_list(self, app_db, mock_settings):
        with appdb.get_conn(app_db) as conn:
            assert qc.run_quality_check_for_user(
                conn, user_id=1, accounts=[], profile_settings=mock_settings,
                window_start=None, window_end=None,
            ) == []


class TestRunJudgeOnMessage:
    def _judge_engine(self):
        engine = MagicMock()
        return engine

    def test_high_confidence_skips_escalation_and_no_summary(self, mock_settings):
        engine = self._judge_engine()
        engine.run_level_1_classification.return_value = (1, "confident", 0.95, "notification", {})
        cached = {
            "message_id": "m1", "sender": "a@b.com", "subject": "hi", "snippet": "snip",
            "email_body": None, "triage_level": 1, "level_2_summary": None,
        }
        result = qc._run_judge_on_message(engine, client=None, profile_settings=mock_settings, cached=cached)
        assert result["judge_level"] == 1
        engine.run_level_1_premium_escalation.assert_not_called()
        assert result["summary_quality_score"] is None

    def test_low_confidence_escalates_using_cached_body(self, mock_settings):
        engine = self._judge_engine()
        engine.run_level_1_classification.return_value = (1, "unsure", 0.4, "notification", {})
        engine.run_level_1_premium_escalation.return_value = (2, "actually important", 0.9, "personal")
        cached = {
            "message_id": "m2", "sender": "a@b.com", "subject": "hi", "snippet": "snip",
            "email_body": "full body text", "triage_level": 1, "level_2_summary": None,
        }
        result = qc._run_judge_on_message(engine, client=None, profile_settings=mock_settings, cached=cached)
        engine.run_level_1_premium_escalation.assert_called_once()
        assert result["judge_level"] == 2

    def test_low_confidence_no_body_and_no_client_skips_escalation(self, mock_settings):
        engine = self._judge_engine()
        engine.run_level_1_classification.return_value = (1, "unsure", 0.4, "notification", {})
        cached = {
            "message_id": "m3", "sender": "a@b.com", "subject": "hi", "snippet": "snip",
            "email_body": None, "triage_level": 1, "level_2_summary": None, "source_id": None,
        }
        result = qc._run_judge_on_message(engine, client=None, profile_settings=mock_settings, cached=cached)
        engine.run_level_1_premium_escalation.assert_not_called()
        assert result["judge_level"] == 1

    def test_scores_summary_only_when_cached_level_2_with_summary(self, mock_settings):
        engine = self._judge_engine()
        engine.run_level_1_classification.return_value = (2, "important", 0.9, "personal", {})
        cached = {
            "message_id": "m4", "sender": "a@b.com", "subject": "hi", "snippet": "snip",
            "email_body": "full body", "triage_level": 2, "level_2_summary": "a summary",
        }
        with patch.object(qc, "_score_summary_quality", return_value={"score": 8.5, "rationale": "good"}) as scorer:
            result = qc._run_judge_on_message(engine, client=None, profile_settings=mock_settings, cached=cached)
        scorer.assert_called_once()
        assert result["summary_quality_score"] == 8.5
        assert result["judge_notes"] == "good"

    def test_no_summary_score_when_no_cached_summary(self, mock_settings):
        engine = self._judge_engine()
        engine.run_level_1_classification.return_value = (2, "important", 0.9, "personal", {})
        cached = {
            "message_id": "m5", "sender": "a@b.com", "subject": "hi", "snippet": "snip",
            "email_body": "full body", "triage_level": 2, "level_2_summary": None,
        }
        with patch.object(qc, "_score_summary_quality") as scorer:
            result = qc._run_judge_on_message(engine, client=None, profile_settings=mock_settings, cached=cached)
        scorer.assert_not_called()
        assert result["summary_quality_score"] is None


class TestResolveWindowStart:
    @pytest.fixture
    def app_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "app.db"
        monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
        appdb.init_app_db(db_path)
        return db_path

    def test_defaults_to_24h_ago_with_no_prior_runs(self, app_db):
        from datetime import datetime, timezone

        window_end = datetime.now(timezone.utc)
        with appdb.get_conn(app_db) as conn:
            start = qc._resolve_window_start(conn, user_id=1, window_end=window_end)
        assert abs((window_end - start).total_seconds() - 86400) < 5

    def test_continues_from_last_successful_run(self, app_db):
        from datetime import datetime, timedelta, timezone

        window_end = datetime.now(timezone.utc)
        last_run_end = window_end - timedelta(hours=3)
        with appdb.get_conn(app_db) as conn:
            conn.execute(
                """INSERT INTO users (id, username, workspace_slug, password_hash, password_salt,
                                       created_at, updated_at)
                   VALUES (1, 'alice', 'alice', 'x', 'y', '2020-01-01', '2020-01-01')"""
            )
            conn.execute(
                """INSERT INTO quality_check_runs
                   (user_id, account, window_start, window_end, sample_rate, population_size, sample_size,
                    started_at, status, created_at)
                   VALUES (1, 'alice@example.com', ?, ?, 0.1, 5, 1, ?, 'ok', ?)""",
                (
                    (last_run_end - timedelta(hours=24)).isoformat(),
                    last_run_end.isoformat(),
                    last_run_end.isoformat(),
                    last_run_end.isoformat(),
                ),
            )
            start = qc._resolve_window_start(conn, user_id=1, window_end=window_end)
        assert start == last_run_end

    def test_ignores_stale_run_older_than_24h(self, app_db):
        from datetime import datetime, timedelta, timezone

        window_end = datetime.now(timezone.utc)
        stale_run_end = window_end - timedelta(days=3)
        with appdb.get_conn(app_db) as conn:
            conn.execute(
                """INSERT INTO users (id, username, workspace_slug, password_hash, password_salt,
                                       created_at, updated_at)
                   VALUES (1, 'alice', 'alice', 'x', 'y', '2020-01-01', '2020-01-01')"""
            )
            conn.execute(
                """INSERT INTO quality_check_runs
                   (user_id, account, window_start, window_end, sample_rate, population_size, sample_size,
                    started_at, status, created_at)
                   VALUES (1, 'alice@example.com', ?, ?, 0.1, 5, 1, ?, 'ok', ?)""",
                (
                    (stale_run_end - timedelta(hours=24)).isoformat(),
                    stale_run_end.isoformat(),
                    stale_run_end.isoformat(),
                    stale_run_end.isoformat(),
                ),
            )
            start = qc._resolve_window_start(conn, user_id=1, window_end=window_end)
        assert abs((window_end - start).total_seconds() - 86400) < 5


class TestRunQualityCheckAllProfiles:
    @pytest.fixture
    def app_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "app.db"
        monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
        appdb.init_app_db(db_path)
        with appdb.get_conn(db_path) as conn:
            conn.execute(
                """INSERT INTO users (id, username, workspace_slug, password_hash, password_salt,
                                       created_at, updated_at, is_active)
                   VALUES (1, 'alice', 'alice', 'x', 'y', '2020-01-01', '2020-01-01', 1)"""
            )
        return db_path

    def _patch_settings(self, monkeypatch, mock_settings):
        from config import Settings

        monkeypatch.setattr(Settings, "load_for_user", classmethod(lambda cls, user, conn=None: mock_settings))

    def test_skips_disabled_user_without_force(self, app_db, mock_settings, monkeypatch):
        mock_settings.quality_check.enabled = False
        self._patch_settings(monkeypatch, mock_settings)
        with patch.object(qc.account_clients, "clients_for_user") as mock_clients:
            result = qc.run_quality_check_all_profiles()
        mock_clients.assert_not_called()
        assert result["accounts"] == []

    def test_force_bypasses_enabled_flag(self, app_db, mock_settings, monkeypatch):
        # This is the real-world bug report this test guards against: an admin
        # clicks "Run now" (force=True) before ever having flipped the nightly
        # schedule's enabled toggle on, and expects it to actually run.
        mock_settings.quality_check.enabled = False
        self._patch_settings(monkeypatch, mock_settings)
        with patch.object(qc.account_clients, "clients_for_user", return_value=[]) as mock_clients:
            qc.run_quality_check_all_profiles(force=True)
        mock_clients.assert_called_once()

    def test_force_still_requires_judge_config(self, app_db, mock_settings, monkeypatch):
        mock_settings.quality_check.enabled = False
        mock_settings.quality_check.judge_base_url = ""
        self._patch_settings(monkeypatch, mock_settings)
        with patch.object(qc.account_clients, "clients_for_user") as mock_clients:
            result = qc.run_quality_check_all_profiles(force=True)
        mock_clients.assert_not_called()
        assert result["accounts"] == []

    def test_enabled_user_runs_without_force(self, app_db, mock_settings, monkeypatch):
        mock_settings.quality_check.enabled = True
        self._patch_settings(monkeypatch, mock_settings)
        with patch.object(qc.account_clients, "clients_for_user", return_value=[]) as mock_clients:
            qc.run_quality_check_all_profiles()
        mock_clients.assert_called_once()


class TestRunQualityCheckForAccount:
    @pytest.fixture
    def app_db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "app.db"
        monkeypatch.setattr(appdb, "DEFAULT_APP_DB_PATH", db_path)
        appdb.init_app_db(db_path)
        with appdb.get_conn(db_path) as conn:
            conn.execute(
                """INSERT INTO users (id, username, workspace_slug, password_hash, password_salt,
                                       created_at, updated_at)
                   VALUES (1, 'alice', 'alice', 'x', 'y', '2020-01-01', '2020-01-01')"""
            )
        return db_path

    @pytest.fixture
    def account_client(self):
        ac = MagicMock()
        ac.account = "alice@example.com"
        ac.provider = "gmail"
        ac.client = MagicMock()
        return ac

    def _seed_cache(self, tmp_path, account, n=4):
        db = EmailDB(db_path=tmp_path / "email_cache.db")
        for i in range(n):
            mid = f"msg-{i}"
            db.upsert_email_metadata(
                message_id=mid, account=account, sender="a@b.com", subject=f"Subject {i}",
                snippet="snippet", source_id=f"src-{i}",
            )
            db.save_triage_result(
                message_id=mid, account=account, sender="a@b.com", subject=f"Subject {i}",
                date_str="2024-01-01", level_0_status="passed", triage_level=1, tag="notification",
            )
        return db

    def test_no_data_when_population_empty(self, app_db, account_client, mock_settings, tmp_path):
        from datetime import datetime, timedelta, timezone

        mock_settings.workspace_dir = tmp_path
        window_end = datetime.now(timezone.utc)
        window_start = window_end - timedelta(hours=24)
        with appdb.get_conn(app_db) as conn:
            result = qc.run_quality_check_for_account(
                conn, user_id=1, account_client=account_client, profile_settings=mock_settings,
                window_start=window_start, window_end=window_end,
            )
        assert result["status"] == "no_data"
        assert result["population_size"] == 0

    def test_error_when_judge_not_configured(self, app_db, account_client, mock_settings, tmp_path):
        from datetime import datetime, timedelta, timezone

        mock_settings.workspace_dir = tmp_path
        mock_settings.quality_check.judge_base_url = ""
        self._seed_cache(tmp_path, account_client.account)
        window_end = datetime.now(timezone.utc)
        window_start = window_end - timedelta(hours=24)
        with appdb.get_conn(app_db) as conn:
            result = qc.run_quality_check_for_account(
                conn, user_id=1, account_client=account_client, profile_settings=mock_settings,
                window_start=window_start, window_end=window_end,
            )
        assert result["status"] == "error"
        assert result["population_size"] == 4

    def test_full_run_persists_metrics_and_items(self, app_db, account_client, mock_settings, tmp_path):
        from datetime import datetime, timedelta, timezone

        mock_settings.workspace_dir = tmp_path
        mock_settings.quality_check.sample_rate = 1.0  # sample everything, deterministic assertions
        self._seed_cache(tmp_path, account_client.account, n=4)
        window_end = datetime.now(timezone.utc)
        window_start = window_end - timedelta(hours=24)

        fake_judge_engine = MagicMock()
        # Cached triage_level is always 1; judge agrees on 2 of the 4, disagrees (says 0) on the other 2.
        fake_judge_engine.run_level_1_classification.side_effect = [
            (1, "r", 0.95, "notification", {}),
            (1, "r", 0.95, "notification", {}),
            (0, "r", 0.95, "low", {}),
            (0, "r", 0.95, "low", {}),
        ]

        with patch.object(qc, "_build_judge_engine", return_value=fake_judge_engine):
            with appdb.get_conn(app_db) as conn:
                result = qc.run_quality_check_for_account(
                    conn, user_id=1, account_client=account_client, profile_settings=mock_settings,
                    window_start=window_start, window_end=window_end,
                )
                items = conn.execute(
                    "SELECT * FROM quality_check_items WHERE run_id = ?", (result["run_id"],)
                ).fetchall()

        assert result["status"] == "ok"
        assert result["sample_size"] == 4
        assert len(items) == 4
        assert result["level_precision"] is not None
        assert result["level_f1"] is not None
