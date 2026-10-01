"""One-time repair for meetings regressed by the pre-V2.0.1 restart scanner."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


TARGETS = (
    "ly-26c9b81477da4fa4a595a6ac74c63461",
    "ly-97d4b0c871a44254bd8fb9359c85ef22",
)


db = sqlite3.connect("/app/data/clearmeeting.db")
db.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()


def snapshot(session_id: str) -> dict:
    meeting = db.execute(
        "SELECT session_id,state,audio_end_ms,updated_at FROM meetings WHERE session_id=?",
        (session_id,),
    ).fetchone()
    device = db.execute(
        "SELECT status,canonical_total_bytes,updated_at FROM device_sessions "
        "WHERE server_session_id=?",
        (session_id,),
    ).fetchone()
    counts = {
        row["state"]: row["n"]
        for row in db.execute(
            "SELECT state,COUNT(*) AS n FROM offline_asr_jobs WHERE session_id=? GROUP BY state",
            (session_id,),
        )
    }
    return {
        "meeting": dict(meeting) if meeting else None,
        "device": dict(device) if device else None,
        "job_counts": counts,
        "pending_gaps": db.execute(
            "SELECT COUNT(*) FROM pending_gaps WHERE session_id=?", (session_id,)
        ).fetchone()[0],
    }


before = {sid: snapshot(sid) for sid in TARGETS}
with db:
    for sid in TARGETS:
        state = before[sid]
        if not state["meeting"] or state["meeting"]["state"] != "finalizing":
            raise RuntimeError(f"{sid}: expected finalizing meeting")
        if not state["device"] or state["device"]["status"] != "done":
            raise RuntimeError(f"{sid}: device is not done")
        if state["job_counts"].get("queued", 0) or state["job_counts"].get("running", 0):
            raise RuntimeError(f"{sid}: active jobs still exist")
        for reason in ("canonical", "publish"):
            if not db.execute(
                "SELECT 1 FROM offline_asr_jobs WHERE session_id=? AND reason=? AND state='done'",
                (sid, reason),
            ).fetchone():
                raise RuntimeError(f"{sid}: {reason} is not done")

        db.execute(
            "UPDATE offline_asr_jobs SET state='cancelled',lease_owner=NULL,lease_until=NULL,"
            "last_error=COALESCE(last_error,'') || ' [obsolete after V2.0.1 recovery repair]',"
            "updated_at=? WHERE session_id=? AND reason='gap' AND state='failed'",
            (now, sid),
        )
        db.execute("DELETE FROM pending_gaps WHERE session_id=?", (sid,))
        changed = db.execute(
            "UPDATE meetings SET state='transcript_ready',updated_at=? "
            "WHERE session_id=? AND state='finalizing'",
            (now, sid),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"{sid}: meeting update count={changed}")

after = {sid: snapshot(sid) for sid in TARGETS}
print(json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2))
db.close()
