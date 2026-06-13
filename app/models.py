from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AudioSource(str, Enum):
    WHATSAPP = "whatsapp"
    RECORDING = "recording"
    OTHER = "other"


class TranscribeAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    poll_url: str


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    user_id: Optional[str] = None
    source: Optional[str] = None
    text: Optional[str] = None
    duration_seconds: Optional[float] = None
    processing_time_seconds: Optional[float] = None
    model: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str
    queue_pending: int = 0


class UserMetricsSummary(BaseModel):
    user_id: str
    job_count: int
    total_duration_seconds: float
    total_processing_time_seconds: float


class MetricsSummaryResponse(BaseModel):
    totals: list[UserMetricsSummary]
    total_jobs: int
    total_duration_seconds: float
    total_processing_time_seconds: float


class ErrorResponse(BaseModel):
    detail: str
