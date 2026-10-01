"""Repair two additional terminal meetings exposed by the V2.0.1 recovery audit."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


TARGETS = (
    "ly-4343d6944bba4db6942572daa6b5676a",
    "ly-d28ffcfbf53a4b2b985e0e21067f47b7",
)

db = sqlite3.connect("/app/data/clearmeeting.db")
db.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
result = {}

with db:
    for sid in TARGETS:
        row = db.execute(
            "SELECT m.state,d.status,d.canonical_total_bytes,"
            "(SELECT COUNT(*) FROM pending_gaps g WHERE g.session_id=m.session_id) AS gaps,"
            "(SELECT COUNT(*) FROM offline_asr_jobs j WHERE j.session_id=m.session_id "
            " AND j.state IN ('queued','running')) AS active,"
            "(SELECT COUNT(*) FROM offline_asr_jobs j WHERE j.session_id=m.session_id "
            " AND j.state='failed') AS failed,"
            "(SELECT COUNT(*) FROM offline_asr_jobs j WHERE j.session_id=m.session_id "
            " AND j.reason='publish' AND j.state='done') AS publish_done,"
            "(SELECT COUNT(*) FROM offline_asr_jobs j WHERE j.session_id=m.session_id "
            " AND j.reason='canonical' AND j.state='done') AS canonical_done "
            "FROM meetings m JOIN device_sessions d ON d.server_session_id=m.session_id "
            "WHERE m.session_id=?",
            (sid,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"{sid}: missing meeting/device")
        before = dict(row)
        canonical_required = row["canonical_total_bytes"] is not None
        eligible = (
            row["state"] == "finalizing"
            and row["status"] == "done"
            and row["gaps"] == 0
            and row["active"] == 0
            and row["failed"] == 0
            and row["publish_done"] > 0
            and (not canonical_required or row["canonical_done"] > 0)
        )
        if not eligible:
            raise RuntimeError(f"{sid}: terminal repair preconditions failed: {before}")
        changed = db.execute(
            "UPDATE meetings SET state='transcript_ready',updated_at=? "
            "WHERE session_id=? AND state='finalizing'",
            (now, sid),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"{sid}: meeting update count={changed}")
        result[sid] = {"before": before, "after": "transcript_ready"}

print(json.dumps(result, ensure_ascii=False, indent=2))
db.close()
