#!/usr/bin/env bash
# Live DB + indexer health probe (Railway dfi-backend SSH).
set -euo pipefail
cd "$(dirname "$0")/../backend"

railway ssh --service dfi-backend -- python - <<'PY'
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import text
from app.db.session import get_session_factory

async def main():
    sf = get_session_factory()
    async with sf() as s:
        counts = dict((await s.execute(text(
            "SELECT status::text, count(*) FROM drive_files GROUP BY 1"
        ))).all())
        processing = (await s.execute(text("""
            SELECT name, path,
                   EXTRACT(EPOCH FROM (now() - processing_started_at))::int AS age_s
            FROM drive_files
            WHERE status = 'PROCESSING'
            ORDER BY processing_started_at NULLS LAST
            LIMIT 8
        """))).mappings().all()
        locks = (await s.execute(text("""
            SELECT count(*) FILTER (WHERE wait_event_type = 'Lock') AS lock_waits,
                   count(*) FILTER (
                     WHERE state = 'idle in transaction'
                       AND xact_start < now() - interval '30 seconds'
                   ) AS idle_in_txn_30s,
                   count(*) FILTER (WHERE state = 'active') AS active,
                   count(*) AS total
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
        """))).mappings().one()
        blockers = (await s.execute(text("""
            SELECT a.pid, left(a.query, 80) AS q,
                   EXTRACT(EPOCH FROM (now() - a.xact_start))::int AS xact_s,
                   a.wait_event_type, a.wait_event, a.state
            FROM pg_stat_activity a
            WHERE a.datname = current_database()
              AND a.pid <> pg_backend_pid()
              AND (
                a.wait_event_type = 'Lock'
                OR (a.state = 'idle in transaction'
                    AND a.xact_start < now() - interval '60 seconds')
              )
            ORDER BY a.xact_start NULLS LAST
            LIMIT 5
        """))).mappings().all()
        recent_err = (await s.execute(text("""
            SELECT name, left(error_message, 90) AS err,
                   last_synced_at AT TIME ZONE 'UTC' AS synced_utc
            FROM drive_files
            WHERE status = 'ERROR'
            ORDER BY last_synced_at DESC NULLS LAST
            LIMIT 4
        """))).mappings().all()
        algo_len = (await s.execute(text("""
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'carousel_generation_saves'
              AND column_name = 'algorithm_version'
        """))).scalar_one()
        face = dict((await s.execute(text(
            "SELECT status::text, count(*) FROM face_jobs GROUP BY 1"
        ))).all())

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "drive_files": counts,
        "processing": [dict(r) for r in processing],
        "pg": dict(locks),
        "blockers": [dict(r) for r in blockers],
        "errors": [dict(r) for r in recent_err],
        "face_jobs": face,
        "algorithm_version_varchar": algo_len,
        "ok": (
            int(locks["lock_waits"] or 0) == 0
            and int(locks["idle_in_txn_30s"] or 0) < 5
            and int(algo_len or 0) >= 64
        ),
    }
    print(json.dumps(out, default=str))

asyncio.run(main())
PY

curl -sS -m 20 "https://dfi-backend-production.up.railway.app/index" \
  | "$(cd "$(dirname "$0")/../backend" && pwd)/.venv/bin/python" -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({"api":{"is_running":d.get("is_running"),"pending":d.get("pending_count"),"current_files":d.get("current_files"),"image_slots":d.get("image_slots"),"video_slots":d.get("video_slots"),"counts":d.get("counts_by_status"),"revision":d.get("revision")}}))'
