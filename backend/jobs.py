from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from paths import JOBS_DIR
from schemas import JobStatus


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Job:
    job_id: str
    job_dir: Path
    source: str
    source_type: str = "local"
    source_value: str = ""
    skip_matching: bool = False
    status: JobStatus = "queued"
    product_count: Optional[int] = None
    error: Optional[str] = None
    message: str = ""
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def json_path(self) -> Path:
        return self.job_dir / "detections.json"

    @property
    def vtt_path(self) -> Path:
        return self.job_dir / "detections.vtt"

    @property
    def zip_path(self) -> Path:
        return self.job_dir / "detections.zip"


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(
        self,
        source: str,
        skip_matching: bool,
        source_type: str = "local",
        source_value: str = "",
    ) -> Job:
        job_id = uuid.uuid4().hex
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(
            job_id=job_id,
            job_dir=job_dir,
            source=source,
            source_type=source_type,
            source_value=source_value,
            skip_matching=skip_matching,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            return job


store = JobStore()
