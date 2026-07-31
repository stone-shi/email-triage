"""Nightly "no-look" production quality audit.

Samples a slice of already-triaged messages (last 24h, or since the previous
scheduled run if more recent), re-evaluates each through a separately
configured judge LLM, and compares the judge's independent result against
what production actually decided. Feeds the admin dashboard's 7-day quality
trend (web_quality_api.py) and is reused as-is by backfill_quality_check.py
for explicit historical windows.

Design notes:
- Precision/recall/F1 treat the judge's re-derived triage level as ground
  truth and the already-cached (production) triage_level as the prediction
  being evaluated -- standard shadow-eval methodology. This only requires the
  judge to independently re-run L1 classification (and, when ambiguous, the
  premium escalation) -- it never needs to know *why* production reached its
  original level (VIP bypass, static blacklist, TEI router, or the LLM), only
  what that final level was.
- There is no "predicted vs actual" pair to diff for free text, so
  summarization quality is scored directly: the judge model grades the
  production summary against the source email on a 1-10 rubric, mirroring
  auto_rater_summarizer.py's approach, rather than generating a second
  competing summary to text-diff against.
- The judge always runs against the *same* prompts.yml/DB-stored system
  prompts as production (only the model/endpoint differ) -- the question this
  feature answers is "would a stronger model reach the same conclusion given
  the same instructions", not "is our prompt wording good".
"""

from __future__ import annotations

import copy
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import account_clients
import appdb
import users_store
from db import EmailDB
from triage import EmailTriageEngine

logger = logging.getLogger("email_triage.quality_check")

_LEVELS = (0, 1, 2)

_SUMMARY_JUDGE_SYSTEM = (
    "You are a strict supervisor auditing an executive assistant's email summaries. Score the "
    "given summary on a 1-10 integer scale across three categories: 'accuracy' (factually true to "
    "the body, no invented details), 'conciseness' (short, crisp, no fluff), and 'actionability' "
    "(clearly surfaces tasks, decisions, and deadlines). Return ONLY a JSON object with fields "
    "'accuracy' (int), 'conciseness' (int), 'actionability' (int), and 'rationale' (string)."
)


class _NullTokenSink:
    """Stand-in for EmailDB passed to a judge-only EmailTriageEngine, so judge LLM calls never
    write into the account's real token_logs table -- judge spend is tracked separately here
    rather than polluting the account's own token-usage dashboard."""

    def __init__(self) -> None:
        self.tokens_used = 0

    def log_token_usage(self, event: str, model: str, tokens_used: int) -> None:
        self.tokens_used += tokens_used or 0


def _build_judge_engine(profile_settings: Any) -> Optional[EmailTriageEngine]:
    """A second EmailTriageEngine pointed at the judge model/endpoint instead of
    the account's real triage/summary models. Returns None if a judge hasn't
    been configured yet (nothing to compare against)."""
    qc = profile_settings.quality_check
    if not qc.judge_base_url or not qc.judge_model:
        return None

    judge_settings = copy.deepcopy(profile_settings)
    judge_settings.triage_base_url = qc.judge_base_url
    judge_settings.triage_api_key = qc.judge_api_key
    judge_settings.triage_model = qc.judge_model
    judge_settings.summary_base_url = qc.judge_base_url
    judge_settings.summary_api_key = qc.judge_api_key
    judge_settings.summary_model = qc.judge_model
    # The judge is always a real LLM call, even if this account's production
    # pipeline routes Level 1 through the reranker (triage.triage_type == "tei").
    judge_settings.triage.triage_type = "llm"

    sink = _NullTokenSink()
    engine = EmailTriageEngine(sink, settings_instance=judge_settings)
    engine._quality_check_token_sink = sink  # type: ignore[attr-defined]
    return engine


def _fetch_body_if_possible(client: Any, cached: Dict[str, Any]) -> Optional[str]:
    source_id = cached.get("source_id")
    if not source_id or client is None:
        return None
    try:
        return client.fetch_full_body(source_id)
    except Exception:
        logger.exception("Quality check: failed to fetch live body for %s", cached.get("message_id"))
        return None


def _score_summary_quality(
    judge_engine: EmailTriageEngine, subject: str, body: str, summary: str
) -> Optional[Dict[str, Any]]:
    """Asks the judge model to grade the production summary 1-10 against the source
    email. Returns {"score": avg-of-3-subscores, "rationale": str} or None on failure."""
    prompt = f"Subject: {subject}\nOriginal body:\n{body[:6000]}\n\nSummary to grade:\n{summary}"
    payload = {
        "model": judge_engine.settings.summary_model,
        "messages": [
            {"role": "system", "content": _SUMMARY_JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "include_reasoning": False,
        "stream": False,
    }
    try:
        url = f"{judge_engine.summary_base_url}/chat/completions"
        response = judge_engine.http_client.post(url, headers=judge_engine.summary_headers, json=payload)
        response.raise_for_status()
        resp_json = response.json()
        usage = resp_json.get("usage", {})
        judge_engine.db.log_token_usage(
            "quality_check_summary_judge", judge_engine.settings.summary_model, usage.get("total_tokens", 0)
        )
        content = resp_json["choices"][0]["message"]["content"]
        data = json.loads(judge_engine._extract_json(content))
        acc = float(data.get("accuracy", 0))
        con = float(data.get("conciseness", 0))
        act = float(data.get("actionability", 0))
        return {"score": round((acc + con + act) / 3.0, 2), "rationale": data.get("rationale")}
    except Exception:
        logger.exception("Quality check: summary judge scoring failed")
        return None


def _run_judge_on_message(
    judge_engine: EmailTriageEngine, client: Any, profile_settings: Any, cached: Dict[str, Any]
) -> Dict[str, Any]:
    """Independently re-derives a final triage level for one already-triaged cached
    row via the judge model (L1 classification, escalating to the judge's premium
    pass only if its own confidence is low), then -- only for items production
    actually summarized -- grades that production summary's quality. Fetches the
    body live (once) only if needed and not already cached."""
    sender = cached.get("sender") or ""
    subject = cached.get("subject") or ""
    snippet = cached.get("snippet") or ""
    body = cached.get("email_body")

    level, reason, score, tag, _metrics = judge_engine.run_level_1_classification(sender, subject, snippet)

    if score < profile_settings.triage.confidence_threshold:
        if not body:
            body = _fetch_body_if_possible(client, cached)
        if body:
            level, reason, score, tag = judge_engine.run_level_1_premium_escalation(sender, subject, snippet, body)

    result: Dict[str, Any] = {
        "judge_level": level,
        "judge_tag": tag,
        "judge_reason": reason,
        "summary_quality_score": None,
        "judge_notes": None,
    }

    cached_summary = cached.get("level_2_summary")
    if cached.get("triage_level") == 2 and cached_summary:
        if not body:
            body = _fetch_body_if_possible(client, cached)
        if body:
            quality = _score_summary_quality(judge_engine, subject, body, cached_summary)
            if quality is not None:
                result["summary_quality_score"] = quality["score"]
                result["judge_notes"] = quality.get("rationale")

    return result


def _macro_prf1(pairs: List[Tuple[Optional[int], Optional[int]]]) -> Dict[str, float]:
    """Macro-averaged precision/recall/F1 over levels {0, 1, 2} from (cached_level,
    judge_level) pairs -- judge_level is treated as ground truth, cached_level as
    the production prediction being evaluated."""
    precisions, recalls = [], []
    for c in _LEVELS:
        tp = sum(1 for pred, actual in pairs if pred == c and actual == c)
        fp = sum(1 for pred, actual in pairs if pred == c and actual != c)
        fn = sum(1 for pred, actual in pairs if pred != c and actual == c)
        if tp + fp > 0:
            precisions.append(tp / (tp + fp))
        if tp + fn > 0:
            recalls.append(tp / (tp + fn))
    precision = sum(precisions) / len(precisions) if precisions else 0.0
    recall = sum(recalls) / len(recalls) if recalls else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


_RUN_DEFAULTS: Dict[str, Any] = {
    "judge_model": None, "level_precision": None, "level_recall": None, "level_f1": None,
    "summary_quality_avg": None, "summary_quality_count": 0, "finished_at": None,
    "status": "ok", "error": None,
}
_ITEM_DEFAULTS: Dict[str, Any] = {
    "cached_level": None, "judge_level": None, "cached_tag": None, "judge_tag": None,
    "cached_summary": None, "judge_summary": None, "summary_quality_score": None, "judge_notes": None,
}


def _save_run(conn, run_row: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    row = {**_RUN_DEFAULTS, **run_row, "created_at": appdb.utcnow()}
    cursor = conn.execute(
        """
        INSERT INTO quality_check_runs
        (user_id, account, window_start, window_end, sample_rate, population_size, sample_size,
         judge_model, level_precision, level_recall, level_f1, summary_quality_avg,
         summary_quality_count, started_at, finished_at, status, error, created_at)
        VALUES (:user_id, :account, :window_start, :window_end, :sample_rate, :population_size, :sample_size,
                :judge_model, :level_precision, :level_recall, :level_f1, :summary_quality_avg,
                :summary_quality_count, :started_at, :finished_at, :status, :error, :created_at)
        """,
        row,
    )
    run_id = cursor.lastrowid
    for item in items:
        item_row = {**_ITEM_DEFAULTS, **item, "run_id": run_id, "created_at": appdb.utcnow()}
        conn.execute(
            """
            INSERT INTO quality_check_items
            (run_id, message_id, cached_level, judge_level, cached_tag, judge_tag, agreement,
             cached_summary, judge_summary, summary_quality_score, judge_notes, created_at)
            VALUES (:run_id, :message_id, :cached_level, :judge_level, :cached_tag, :judge_tag, :agreement,
                    :cached_summary, :judge_summary, :summary_quality_score, :judge_notes, :created_at)
            """,
            item_row,
        )
    return {"run_id": run_id, **row}


def _stratified_sample(population: List[Dict[str, Any]], sample_rate: float) -> List[Dict[str, Any]]:
    """Samples sample_rate of `population` independently within each triage_level group,
    rather than one flat random draw across the whole population -- a flat draw at a small
    sample_rate can easily miss an entire level by chance (e.g. a 10% draw that happens to land
    only on level 1 when level 0/2 are numerically smaller), which defeats the point of auditing
    all three. Rounds each level's quota to the nearest whole message with a floor of 1 (never 0)
    for any level that has at least one message, so e.g. 20/50/30 messages at a 10% rate become
    2/5/3 sampled -- matching the level's share of the total, not an arbitrary flat count."""
    sample_rate = max(0.0, min(1.0, sample_rate))
    by_level: Dict[Any, List[Dict[str, Any]]] = {}
    for row in population:
        by_level.setdefault(row.get("triage_level"), []).append(row)

    sampled: List[Dict[str, Any]] = []
    for rows in by_level.values():
        n = len(rows)
        take = min(n, max(1, round(n * sample_rate)))
        sampled.extend(rows if take >= n else random.sample(rows, take))
    return sampled


def run_quality_check_for_user(
    conn,
    user_id: int,
    accounts: List["account_clients.AccountClient"],
    profile_settings: Any,
    *,
    window_start: datetime,
    window_end: datetime,
) -> List[Dict[str, Any]]:
    """Pools every one of a user's accounts' already-triaged messages together before sampling,
    so quality_check.sample_rate is applied against the user's total combined volume rather than
    reapplied independently per account -- a low-volume account would otherwise round its own
    sample down to zero every night even though the user's overall traffic clearly warrants
    checking some of it. Still persists one quality_check_runs row per account (see
    run_quality_check_for_account) for the existing per-account drill-down -- only the sampling
    pool changes, not the reporting granularity. `conn` is an open data/app.db connection --
    caller owns committing/closing it."""
    if not accounts:
        return []

    db = EmailDB(settings_instance=profile_settings)
    combined_population = db.get_triaged_messages_in_window(
        [ac.account for ac in accounts], window_start.isoformat(), window_end.isoformat()
    )
    sample_rate = profile_settings.quality_check.sample_rate
    combined_sample = _stratified_sample(combined_population, sample_rate)

    population_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for row in combined_population:
        population_by_account.setdefault(row["account"], []).append(row)
    sample_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for row in combined_sample:
        sample_by_account.setdefault(row["account"], []).append(row)

    return [
        run_quality_check_for_account(
            conn, user_id, ac, profile_settings,
            window_start=window_start, window_end=window_end,
            population=population_by_account.get(ac.account, []),
            sample=sample_by_account.get(ac.account, []),
        )
        for ac in accounts
    ]


def run_quality_check_for_account(
    conn,
    user_id: int,
    account_client: "account_clients.AccountClient",
    profile_settings: Any,
    *,
    window_start: datetime,
    window_end: datetime,
    population: Optional[List[Dict[str, Any]]] = None,
    sample: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Judges a sample of the messages triaged for one account within [window_start, window_end)
    and persists one quality_check_runs row (+ per-message quality_check_items rows). `conn` is an
    open data/app.db connection -- caller owns committing/closing it.

    `population`/`sample` let run_quality_check_for_user pass in an already-selected,
    cross-account-stratified sample (see _stratified_sample) instead of this function computing
    its own -- pass both together, or leave both None to have this function query and
    stratify-sample within just this one account (used by tests exercising a single account in
    isolation; both real callers, run_quality_check_all_profiles and backfill_quality_check.py,
    go through run_quality_check_for_user so sampling is always pooled across a user's accounts)."""
    account = account_client.account
    db = EmailDB(settings_instance=profile_settings)
    started_at = appdb.utcnow()

    if population is None:
        population = db.get_triaged_messages_in_window(account, window_start.isoformat(), window_end.isoformat())
    sample_rate = max(0.0, min(1.0, profile_settings.quality_check.sample_rate))
    if sample is None:
        sample = _stratified_sample(population, sample_rate)

    run_row: Dict[str, Any] = {
        "user_id": user_id,
        "account": account,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "sample_rate": sample_rate,
        "population_size": len(population),
        "sample_size": len(sample),
        "judge_model": profile_settings.quality_check.judge_model,
        "started_at": started_at,
    }

    if not sample:
        run_row.update(status="no_data", finished_at=appdb.utcnow())
        return _save_run(conn, run_row, items=[])

    judge_engine = _build_judge_engine(profile_settings)
    if judge_engine is None:
        run_row.update(
            status="error",
            error="quality_check.judge_base_url/judge_model not configured",
            finished_at=appdb.utcnow(),
        )
        return _save_run(conn, run_row, items=[])

    items: List[Dict[str, Any]] = []
    pairs: List[Tuple[Optional[int], Optional[int]]] = []
    quality_scores: List[float] = []

    for cached in sample:
        try:
            judged = _run_judge_on_message(judge_engine, account_client.client, profile_settings, cached)
        except Exception:
            logger.exception("Quality check: judge run failed for message %s", cached.get("message_id"))
            continue

        cached_level = cached.get("triage_level")
        judge_level = judged["judge_level"]
        pairs.append((cached_level, judge_level))
        items.append({
            "message_id": cached["message_id"],
            "cached_level": cached_level,
            "judge_level": judge_level,
            "cached_tag": cached.get("tag"),
            "judge_tag": judged["judge_tag"],
            "agreement": 1 if cached_level == judge_level else 0,
            "cached_summary": cached.get("level_2_summary"),
            "summary_quality_score": judged["summary_quality_score"],
            "judge_notes": judged["judge_notes"],
        })
        if judged["summary_quality_score"] is not None:
            quality_scores.append(judged["summary_quality_score"])

    metrics = _macro_prf1(pairs) if pairs else {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    run_row.update(
        status="ok",
        level_precision=metrics["precision"],
        level_recall=metrics["recall"],
        level_f1=metrics["f1"],
        summary_quality_avg=round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else None,
        summary_quality_count=len(quality_scores),
        finished_at=appdb.utcnow(),
    )
    return _save_run(conn, run_row, items=items)


def _resolve_window_start(conn, user_id: int, window_end: datetime) -> datetime:
    """The end of this user's most recent successful run, if there was one and it's
    more recent than 24h ago; otherwise 24h before window_end. This is what lets the
    nightly job cover exactly "since last time" without gaps or overlap, while still
    defaulting sensibly on day one."""
    row = conn.execute(
        "SELECT MAX(window_end) AS last FROM quality_check_runs WHERE user_id = ? AND status = 'ok'",
        (user_id,),
    ).fetchone()
    default_start = window_end - timedelta(hours=24)
    if row is None or row["last"] is None:
        return default_start
    try:
        last = datetime.fromisoformat(row["last"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return default_start
    return max(last, default_start) if last < window_end else default_start


def run_quality_check_all_profiles(window_end: Optional[datetime] = None, *, force: bool = False) -> Dict[str, Any]:
    """Entry point for the nightly scheduler tick and the manual "run now" admin
    trigger. Iterates every active data/app.db user with a configured judge;
    profiles that haven't been migrated into data/app.db yet are skipped, since
    there's nowhere admin-only to configure or view their results.

    `force=True` (the manual trigger) still requires a judge base_url/model to
    be configured -- there's nothing to compare against otherwise -- but skips
    the `quality_check.enabled` check, since that flag only controls whether
    the *automatic nightly schedule* fires, not whether an admin's explicit
    "Run now" click should do anything.
    """
    from config import Settings

    results: Dict[str, Any] = {"accounts": []}
    if not appdb.DEFAULT_APP_DB_PATH.exists():
        logger.info("Quality check: data/app.db does not exist; nothing to do.")
        return results

    if window_end is None:
        window_end = datetime.now(timezone.utc)

    logger.info("Quality check run starting (force=%s, window_end=%s)", force, window_end.isoformat())

    with appdb.get_conn() as conn:
        users = users_store.list_active_users(conn)
        for user_row in users:
            username = user_row["username"]
            try:
                profile_settings = Settings.load_for_user(user_row["id"], conn=conn)
            except Exception:
                logger.exception("Quality check: failed to load settings for user %s", username)
                continue

            qc = profile_settings.quality_check
            if not force and not qc.enabled:
                logger.info("Quality check: skipping %s (quality_check.enabled is off)", username)
                continue
            if not qc.judge_base_url or not qc.judge_model:
                logger.info(
                    "Quality check: skipping %s (quality_check.judge_base_url/judge_model not configured)", username
                )
                continue

            window_start = _resolve_window_start(conn, user_row["id"], window_end)
            try:
                accounts = account_clients.clients_for_user(conn, user_row["id"], profile_settings, for_triage=True)
            except Exception:
                logger.exception("Quality check: failed to resolve accounts for user %s", username)
                continue
            if not accounts:
                logger.info("Quality check: skipping %s (no triage-enabled accounts)", username)
                continue

            logger.info(
                "Quality check: running %s over %d account(s) (pooled sampling), %s -> %s",
                username, len(accounts), window_start.isoformat(), window_end.isoformat(),
            )
            try:
                runs = run_quality_check_for_user(
                    conn, user_row["id"], accounts, profile_settings,
                    window_start=window_start, window_end=window_end,
                )
                for ac, run in zip(accounts, runs):
                    results["accounts"].append({"user": username, "account": ac.account, **run})
                    logger.info(
                        "Quality check: %s / %s -> status=%s sample=%s/%s f1=%s",
                        username, ac.account, run.get("status"), run.get("sample_size"),
                        run.get("population_size"), run.get("level_f1"),
                    )
            except Exception:
                logger.exception("Quality check failed for user %s", username)
            finally:
                conn.commit()

    logger.info("Quality check run finished: %d account run(s) recorded.", len(results["accounts"]))
    return results
