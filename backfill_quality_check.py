#!/usr/bin/env python3
"""One-off backfill of the nightly production quality check (quality_check.py)
for the past N days, split into explicit day-by-day windows -- useful for
populating the dashboard's 7-day trend chart before the nightly scheduler has
had time to accumulate real history, or to re-run a range after fixing a
judge-model configuration mistake.

Requires data/app.db to exist (this feature is DB-backed-user only) and a
judge_base_url/judge_model to be configured (System Settings -> Quality
check). Unlike the nightly scheduler, this does NOT require
quality_check.enabled to be on -- like the dashboard's "Run now" button, a
deliberate manual invocation shouldn't be gated by the automatic-schedule
toggle.

Usage:
    ./venv/bin/python3 backfill_quality_check.py                 # last 7 days, all active users
    ./venv/bin/python3 backfill_quality_check.py --days 3
    ./venv/bin/python3 backfill_quality_check.py --profile jenny --days 14
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import account_clients
import appdb
import users_store
from config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] backfill_quality_check: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill_quality_check")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill N days of nightly quality-check runs")
    parser.add_argument("--days", type=int, default=7, help="How many past days to backfill (default: 7)")
    parser.add_argument(
        "--profile", type=str, default=None, help="Only backfill this username (default: every active user)"
    )
    args = parser.parse_args()

    if not appdb.DEFAULT_APP_DB_PATH.exists():
        logger.error("data/app.db does not exist -- nothing to backfill (this feature is DB-backed-user only).")
        sys.exit(1)

    # Imported lazily so a missing data/app.db (handled above) fails before this module,
    # which touches quality_check_runs/quality_check_items, is even loaded.
    import quality_check

    with appdb.get_conn() as conn:
        users = users_store.list_active_users(conn)
        if args.profile:
            users = [u for u in users if u["username"] == args.profile]
            if not users:
                logger.error("No active user found with username %r", args.profile)
                sys.exit(1)

        now = datetime.now(timezone.utc)
        total_runs = 0

        for user_row in users:
            profile_settings = Settings.load_for_user(user_row["id"], conn=conn)
            qc = profile_settings.quality_check
            if not qc.judge_base_url or not qc.judge_model:
                logger.warning(
                    "Skipping %s: quality_check.judge_base_url/judge_model not configured",
                    user_row["username"],
                )
                continue

            try:
                accounts = account_clients.clients_for_user(conn, user_row["id"], profile_settings, for_triage=True)
            except Exception:
                logger.exception("Failed to resolve accounts for %s; skipping", user_row["username"])
                continue
            if not accounts:
                logger.warning("Skipping %s: no triage-enabled accounts", user_row["username"])
                continue

            for day_offset in range(args.days, 0, -1):
                window_start = now - timedelta(days=day_offset)
                window_end = now - timedelta(days=day_offset - 1)
                logger.info(
                    "Backfilling %s over %d account(s) (pooled sampling) for %s -> %s",
                    user_row["username"], len(accounts), window_start.isoformat(), window_end.isoformat(),
                )
                try:
                    results = quality_check.run_quality_check_for_user(
                        conn, user_row["id"], accounts, profile_settings,
                        window_start=window_start, window_end=window_end,
                    )
                    for ac, result in zip(accounts, results):
                        total_runs += 1
                        logger.info(
                            "  -> %s: status=%s sample=%s/%s f1=%s summary_quality=%s",
                            ac.account, result.get("status"), result.get("sample_size"),
                            result.get("population_size"), result.get("level_f1"), result.get("summary_quality_avg"),
                        )
                except Exception:
                    logger.exception("  -> failed")
                finally:
                    conn.commit()

    logger.info("Backfill complete: %d run(s) written.", total_runs)


if __name__ == "__main__":
    main()
