from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "completed", "failed"]


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus = "queued"
    status_url: str
    json_url: str
    vtt_url: str
    zip_url: str


class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    source: str = ""
    skip_matching: bool = False
    product_count: Optional[int] = None
    error: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    json_url: Optional[str] = None
    vtt_url: Optional[str] = None
    zip_url: Optional[str] = None
    message: str = Field(default="")


class HealthResponse(BaseModel):
    ok: bool
    cuda: bool
    device: str
    models_loaded: bool
    yolo_weights: str
    clip_model: str
    realesrgan: bool
    models_dir: str
