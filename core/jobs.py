"""下载任务状态机。状态放服务端，刷新页面能接管进行中的任务。"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
import uuid

_LOCK = threading.RLock()
_JOBS: dict[str, "Job"] = {}

STAGES = [
    ("queued", "排队中"),
    ("downloading", "下载中"),
    ("merging", "合并音轨"),
    ("done", "已完成"),
]


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str, payload: dict):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.payload = payload or {}
        self.created = time.time()
        self.updated = self.created
        self.stage = "queued"
        self.stage_label = "排队中"
        self.progress = 0.0
        self.message = "等待开始"
        self.speed = ""
        self.eta = ""
        self.total = ""
        self.error = ""
        self.cancelled = False
        self.result: dict = {}
        self.logs: list[str] = []
        self.logs = []
        self.proc = None
        self._cond = threading.Condition()

    # ---------- 状态 ----------
    def _touch(self) -> None:
        self.updated = time.time()
        self._cond.notify_all()

    def set_stage(self, stage: str, label: str = "", message: str = "") -> None:
        with self._cond:
            self.stage = stage
            self.stage_label = label or dict(STAGES).get(stage, stage)
            if message:
                self.message = message
            self._touch()

    def set_progress(self, p: float, **kw) -> None:
        with self._cond:
            self.progress = max(0.0, min(1.0, float(p)))
            for k, v in kw.items():
                if v is not None:
                    setattr(self, k, v)
            self._touch()

    def note(self, text: str) -> None:
        with self._cond:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            self.logs = self.logs[-200:]
            self.message = text
            self._touch()

    def fail(self, exc: BaseException) -> None:
        with self._cond:
            self.error = str(exc) or type(exc).__name__
            self.stage = "error"
            self.stage_label = "失败"
            self.logs.append(traceback.format_exc()[-2000:])
            self._touch()

    def finish(self, result: dict) -> None:
        with self._cond:
            self.result = result
            self.stage = "done"
            self.stage_label = "已完成"
            self.progress = 1.0
            self.speed = ""
            self.eta = ""
            self._touch()

    def cancel(self) -> None:
        with self._cond:
            self.cancelled = True
            self._touch()

    def check_cancel(self) -> None:
        if self.cancelled:
            raise Cancelled("已取消")

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "id": self.id,
                "kind": self.kind,
                "stage": self.stage,
                "stage_label": self.stage_label,
                "progress": round(self.progress, 4),
                "message": self.message,
                "speed": self.speed,
                "eta": self.eta,
                "total": self.total,
                "error": self.error,
                "cancelled": self.cancelled,
                "result": dict(self.result),
                "payload": dict(self.payload),
                "logs": self.logs[-40:],
                "created": self.created,
                "updated": self.updated,
            }

    def wait_change(self, last: float, timeout: float = 10.0) -> dict:
        with self._cond:
            if self.updated <= last:
                self._cond.wait(timeout)
            return self.snapshot()


def create(kind: str, payload: dict) -> Job:
    job = Job(kind, payload)
    with _LOCK:
        _JOBS[job.id] = job
        if len(_JOBS) > 40:
            for jid in sorted(_JOBS, key=lambda k: _JOBS[k].created)[:-40]:
                _JOBS.pop(jid, None)
    return job


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def recent(n: int = 20) -> list[Job]:
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.created, reverse=True)
    return jobs[:n]


def active_count() -> int:
    with _LOCK:
        return len([j for j in _JOBS.values() if j.stage in ("queued", "downloading", "merging")])
