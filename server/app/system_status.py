"""Authenticated, read-only system status for the Settings dashboard."""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .auth import CurrentUser, require_auth
from .speaker_diarizer import probe_speaker_backend


def _memory() -> dict[str, int | float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {"total_bytes": 0, "available_bytes": 0, "used_percent": 0.0}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used_percent = round((1 - available / total) * 100, 1) if total else 0.0
    return {"total_bytes": total, "available_bytes": available,
            "used_percent": used_percent}


def _uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _load_average() -> tuple[float, float, float]:
    if hasattr(os, "getloadavg"):
        return os.getloadavg()
    try:
        values = Path("/proc/loadavg").read_text(encoding="ascii").split()[:3]
        return tuple(float(value) for value in values)  # type: ignore[return-value]
    except (OSError, ValueError):
        return 0.0, 0.0, 0.0


async def _probe_url(raw_url: str, fallback: str) -> dict[str, Any]:
    parsed = urlparse(raw_url or fallback)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    if not host:
        return {"ready": False, "detail": "地址未配置"}
    started = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=0.8)
        writer.close()
        await writer.wait_closed()
        return {"ready": True,
                "latency_ms": max(1, round((time.monotonic() - started) * 1000))}
    except (OSError, asyncio.TimeoutError):
        return {"ready": False, "detail": "服务端口不可达"}


class FailedJobAction(BaseModel):
    action: Literal["retry", "skip"]


def _queue_snapshot(storage: Any, user_id: str) -> dict[str, Any]:
    counts = {state: 0 for state in ("queued", "running", "done", "failed", "cancelled")}
    for row in storage.db.query(
            "SELECT state,COUNT(*) AS n FROM offline_asr_jobs GROUP BY state"):
        state = str(row["state"])
        if state in counts:
            counts[state] = int(row["n"])

    rows = storage.db.query(
        "SELECT j.session_id,COALESCE(NULLIF(TRIM(m.title),''),'未命名会议') AS title,"
        "m.state AS meeting_state,m.created_at AS meeting_created_at,COUNT(*) AS total,"
        "SUM(CASE WHEN j.state='done' THEN 1 ELSE 0 END) AS done,"
        "SUM(CASE WHEN j.state='queued' THEN 1 ELSE 0 END) AS queued,"
        "SUM(CASE WHEN j.state='running' THEN 1 ELSE 0 END) AS running,"
        "SUM(CASE WHEN j.state='failed' THEN 1 ELSE 0 END) AS failed,"
        "SUM(CASE WHEN j.state='cancelled' THEN 1 ELSE 0 END) AS cancelled,"
        "MAX(j.updated_at) AS updated_at "
        "FROM offline_asr_jobs j JOIN meetings m ON m.session_id=j.session_id "
        "WHERE m.owner_user_id=? GROUP BY j.session_id,m.title,m.state,m.created_at "
        "ORDER BY CASE WHEN SUM(CASE WHEN j.state IN ('queued','running','failed') "
        "THEN 1 ELSE 0 END)>0 THEN 0 ELSE 1 END,MAX(j.updated_at) DESC LIMIT 8",
        (user_id,))
    meetings = []
    for row in rows:
        item = {key: int(row[key] or 0) for key in
                ("total", "done", "queued", "running", "failed", "cancelled")}
        if item["failed"]:
            state = "failed"
        elif item["running"]:
            state = "running"
        elif item["queued"]:
            state = "queued"
        elif item["done"]:
            state = "done"
        else:
            state = "cancelled"
        finished = item["done"] + item["cancelled"]
        item.update({
            "session_id": str(row["session_id"]),
            "title": str(row["title"]),
            "meeting_state": str(row["meeting_state"]),
            "meeting_created_at": row["meeting_created_at"],
            "state": state,
            "progress_percent": round(finished * 100 / item["total"])
            if item["total"] else 0,
            "updated_at": row["updated_at"],
        })
        meetings.append(item)
    failed_rows = storage.db.query(
        "SELECT j.id,j.session_id,j.start_ms,j.end_ms,j.attempts,j.last_error,j.updated_at "
        "FROM offline_asr_jobs j JOIN meetings m ON m.session_id=j.session_id "
        "WHERE m.owner_user_id=? AND j.state='failed' ORDER BY j.updated_at DESC",
        (user_id,))
    failed_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in failed_rows:
        failed_by_session.setdefault(str(row["session_id"]), []).append({
            "id": int(row["id"]),
            "start_ms": int(row["start_ms"]),
            "end_ms": int(row["end_ms"]),
            "attempts": int(row["attempts"]),
            "error": str(row["last_error"] or "识别服务未返回结果"),
            "updated_at": row["updated_at"],
        })
    for meeting in meetings:
        meeting["failed_jobs"] = failed_by_session.get(meeting["session_id"], [])
    return {"counts": counts, "meetings": meetings}


def create_system_status_router(storage: Any, *, server_version: str,
                                server_release: str, offline_queue: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/system", tags=["system-status"])
    data_dir = Path(os.getenv("DATA_DIR", "server/data"))

    @router.get("/status")
    async def system_status(response: Response,
                            user: CurrentUser = Depends(require_auth)):
        online, offline, speaker = await asyncio.gather(
            _probe_url(os.getenv("FUNASR_WS_URL", ""), "ws://funasr:10095"),
            _probe_url(os.getenv("FUNASR_OFFLINE_HTTP_URL", ""),
                       "http://funasr-offline-cpu:10096"),
            probe_speaker_backend(), return_exceptions=True)
        if isinstance(speaker, Exception):
            speaker = {"ready": False, "detail": "声纹服务检查失败"}
        if isinstance(online, Exception):
            online = {"ready": False, "detail": "在线转写检查失败"}
        if isinstance(offline, Exception):
            offline = {"ready": False, "detail": "离线转写检查失败"}

        queue = _queue_snapshot(storage, user.id)
        disk = shutil.disk_usage(data_dir if data_dir.exists() else "/")
        load_1m, load_5m, load_15m = _load_average()
        cpu_count = os.cpu_count() or 1
        services = {
            "api": {"ready": True},
            "online_asr": online,
            "offline_asr": offline,
            "speaker": speaker,
        }
        unavailable = [name for name, service in services.items()
                       if not bool(service.get("ready"))]
        attention = any(item["failed"] for item in queue["meetings"])
        active = queue["counts"]["queued"] + queue["counts"]["running"]
        overall = "degraded" if unavailable else (
            "attention" if attention else ("busy" if active else "healthy"))
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": overall,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": {"server_version": server_version,
                        "server_release": server_release},
            "host": {
                "cpu_count": cpu_count,
                "load_1m": round(load_1m, 2),
                "load_5m": round(load_5m, 2),
                "load_15m": round(load_15m, 2),
                "load_percent": round(load_1m * 100 / cpu_count, 1),
                "memory": _memory(),
                "disk": {"total_bytes": disk.total, "free_bytes": disk.free,
                         "used_percent": round(disk.used * 100 / disk.total, 1)},
                "uptime_seconds": _uptime_seconds(),
            },
            "services": services,
            "offline": queue,
        }

    @router.post("/offline-jobs/{job_id}/actions")
    async def failed_job_action(job_id: int, body: FailedJobAction,
                                user: CurrentUser = Depends(require_auth)):
        row = storage.db.query_one(
            "SELECT j.id,j.session_id,j.state,m.owner_user_id FROM offline_asr_jobs j "
            "JOIN meetings m ON m.session_id=j.session_id WHERE j.id=?", (job_id,))
        if row is None or str(row["owner_user_id"]) != user.id:
            raise HTTPException(status_code=404, detail="没有找到该离线任务")
        if str(row["state"]) != "failed":
            raise HTTPException(status_code=409, detail="该任务已不再处于失败状态")
        session_id = str(row["session_id"])
        now = datetime.now(timezone.utc).isoformat()
        with storage.db.transaction() as conn:
            if body.action == "retry":
                conn.execute(
                    "UPDATE offline_asr_jobs SET state='queued',attempts=0,transient_attempts=0,"
                    "available_at=?,lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? "
                    "WHERE id=? AND state='failed'", (now, now, job_id))
            else:
                conn.execute(
                    "UPDATE offline_asr_jobs SET state='cancelled',lease_owner=NULL,lease_until=NULL,"
                    "last_error='skipped_by_user',updated_at=? WHERE id=? AND state='failed'",
                    (now, job_id))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM offline_asr_jobs WHERE session_id=? AND state='failed'",
                (session_id,)).fetchone()[0]
            if not remaining:
                conn.execute(
                    "UPDATE offline_asr_jobs SET state='queued',attempts=0,transient_attempts=0,"
                    "available_at=?,lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? "
                    "WHERE session_id=? AND reason IN ('publish','summarize') AND state='cancelled'",
                    (now, now, session_id))
            conn.execute(
                "UPDATE device_sessions SET status='processing',failure_code=NULL,"
                "failure_message=NULL,updated_at=? WHERE server_session_id=? AND status='failed'",
                (now, session_id))
            conn.execute(
                "UPDATE meetings SET state='finalizing',updated_at=? WHERE session_id=?",
                (now, session_id))
        if offline_queue is not None:
            offline_queue.wake()
        return {"ok": True, "action": body.action, "job_id": job_id,
                "session_id": session_id, "remaining_failed": int(remaining),
                "publish_released": not bool(remaining)}

    return router
