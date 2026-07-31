"""Admin-only read routes for the nightly production quality check (see
quality_check.py): a 7-day trend for the dashboard chart and a per-account
run list for drill-down. The manual "run now" trigger lives in mcp_server.py
alongside the sync/download-all triggers, since it needs that module's
threading lock -- this file only ever reads.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from web_auth import requires_admin


def _daily_trend(conn: sqlite3.Connection, days: int) -> List[Dict[str, Any]]:
    """One entry per calendar day (UTC, zero-filled), each metric a weighted average across
    every account's run that landed that day -- weighted by sample_size for precision/recall/F1,
    and by summary_quality_count for the summary-quality average, so a run with more sampled
    messages counts more. Also reports run_count/error_count/no_data_count (across every status,
    not just 'ok') so an admin can tell "nothing ran that day" apart from "it ran and failed" --
    both would otherwise look identical (blank metrics) if only 'ok' runs were counted at all."""
    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=days - 1)
    buckets: Dict[str, Dict[str, float]] = {
        (start_day + timedelta(days=i)).isoformat(): {
            "precision_sum": 0.0, "recall_sum": 0.0, "f1_sum": 0.0, "weight": 0.0,
            "quality_sum": 0.0, "quality_weight": 0.0,
            "run_count": 0, "error_count": 0, "no_data_count": 0,
        }
        for i in range(days)
    }

    rows = conn.execute(
        """SELECT window_end, status, sample_size, level_precision, level_recall, level_f1,
                  summary_quality_avg, summary_quality_count
           FROM quality_check_runs
           WHERE SUBSTR(window_end, 1, 10) >= ?""",
        (start_day.isoformat(),),
    ).fetchall()

    for row in rows:
        day = row["window_end"][:10]
        bucket = buckets.get(day)
        if bucket is None:
            continue
        bucket["run_count"] += 1
        if row["status"] == "error":
            bucket["error_count"] += 1
            continue
        if row["status"] == "no_data":
            bucket["no_data_count"] += 1
            continue
        weight = row["sample_size"] or 0
        if weight > 0 and row["level_f1"] is not None:
            bucket["precision_sum"] += row["level_precision"] * weight
            bucket["recall_sum"] += row["level_recall"] * weight
            bucket["f1_sum"] += row["level_f1"] * weight
            bucket["weight"] += weight
        qweight = row["summary_quality_count"] or 0
        if qweight > 0 and row["summary_quality_avg"] is not None:
            bucket["quality_sum"] += row["summary_quality_avg"] * qweight
            bucket["quality_weight"] += qweight

    out = []
    for day, b in sorted(buckets.items()):
        out.append({
            "day": day,
            "precision": round(b["precision_sum"] / b["weight"], 4) if b["weight"] else None,
            "recall": round(b["recall_sum"] / b["weight"], 4) if b["weight"] else None,
            "f1": round(b["f1_sum"] / b["weight"], 4) if b["weight"] else None,
            "summary_quality_avg": round(b["quality_sum"] / b["quality_weight"], 2) if b["quality_weight"] else None,
            "sample_size": int(b["weight"]),
            "run_count": int(b["run_count"]),
            "error_count": int(b["error_count"]),
            "no_data_count": int(b["no_data_count"]),
        })
    return out


def register_quality_routes(mcp) -> None:
    @mcp.custom_route("/api/quality/trend", methods=["GET"])
    @requires_admin
    async def quality_trend(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        days = max(1, min(90, int(request.query_params.get("days", 7))))
        return JSONResponse({"days": _daily_trend(conn, days)})

    @mcp.custom_route("/api/quality/runs", methods=["GET"])
    @requires_admin
    async def quality_runs(request: Request) -> Response:
        conn: sqlite3.Connection = request.state.conn
        days = max(1, min(90, int(request.query_params.get("days", 7))))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """SELECT qr.*, u.username FROM quality_check_runs qr
               JOIN users u ON u.id = qr.user_id
               WHERE qr.window_end >= ?
               ORDER BY qr.window_end DESC
               LIMIT 200""",
            (cutoff,),
        ).fetchall()
        return JSONResponse({"runs": [dict(r) for r in rows]})
