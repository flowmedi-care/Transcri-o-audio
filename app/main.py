from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.audio_utils import AudioValidationError
from app.auth import verify_api_key
from app.config import Settings, get_settings
from app.models import (
    AudioSource,
    HealthResponse,
    JobResponse,
    JobStatus,
    MetricsSummaryResponse,
    TranscribeAcceptedResponse,
    UserMetricsSummary,
)
from app.queue import JobQueue, RateLimitError, get_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue = get_queue()
    await queue.start()
    yield
    await queue.stop()


app = FastAPI(
    title="Transcribe API",
    version="1.0.0",
    description="Async audio transcription API for SaaS integrations",
    lifespan=lifespan,
)

DEV_PLAYGROUND = Path(__file__).resolve().parent.parent / "dev" / "playground.html"


def _setup_dev_ui(fastapi_app: FastAPI) -> None:
    settings = get_settings()
    if not settings.dev_ui_enabled:
        return

    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @fastapi_app.get("/dev", include_in_schema=False)
    async def dev_playground() -> FileResponse:
        return FileResponse(DEV_PLAYGROUND)


_setup_dev_ui(app)


def _job_to_response(job: dict) -> JobResponse:
    status_value = JobStatus(job["status"])
    transcript: Optional[str] = job.get("transcript")

    return JobResponse(
        job_id=job["id"],
        status=status_value,
        user_id=job.get("user_id"),
        source=job.get("source"),
        text=transcript if status_value == JobStatus.COMPLETED else None,
        duration_seconds=job.get("duration_seconds"),
        processing_time_seconds=job.get("processing_time_seconds"),
        model=job.get("model"),
        error_message=job.get("error_message"),
        created_at=job.get("created_at"),
    )


@app.get("/health", response_model=HealthResponse)
async def health(
    settings: Settings = Depends(get_settings),
    queue: JobQueue = Depends(get_queue),
) -> HealthResponse:
    pending = await queue.pending_count()
    return HealthResponse(status="ok", model=settings.whisper_model, queue_pending=pending)


@app.post(
    "/v1/transcribe",
    response_model=TranscribeAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def transcribe(
    user_id: str = Form(..., min_length=1, max_length=128),
    source: Optional[AudioSource] = Form(default=None),
    file: UploadFile = File(...),
    _: str = Depends(verify_api_key),
    queue: JobQueue = Depends(get_queue),
) -> TranscribeAcceptedResponse:
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing filename")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio file")

    try:
        result = await queue.enqueue(
            user_id=user_id,
            source=source.value if source else None,
            original_filename=file.filename,
            raw_bytes=raw_bytes,
        )
    except RateLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except AudioValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return TranscribeAcceptedResponse(
        job_id=result.job_id,
        status=result.status,
        poll_url=f"/v1/jobs/{result.job_id}",
    )


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    _: str = Depends(verify_api_key),
    queue: JobQueue = Depends(get_queue),
) -> JobResponse:
    job = await queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _job_to_response(job)


@app.get("/v1/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(
    _: str = Depends(verify_api_key),
    queue: JobQueue = Depends(get_queue),
) -> MetricsSummaryResponse:
    rows = await queue.storage.get_metrics_summary()

    aggregates: dict[str, dict[str, float | int]] = {}
    for row in rows:
        user_id = row.get("user_id") or "unknown"
        bucket = aggregates.setdefault(
            user_id,
            {"job_count": 0, "total_duration_seconds": 0.0, "total_processing_time_seconds": 0.0},
        )
        bucket["job_count"] += 1
        bucket["total_duration_seconds"] += float(row.get("duration_seconds") or 0)
        bucket["total_processing_time_seconds"] += float(row.get("processing_time_seconds") or 0)

    totals = [
        UserMetricsSummary(
            user_id=user_id,
            job_count=int(values["job_count"]),
            total_duration_seconds=float(values["total_duration_seconds"]),
            total_processing_time_seconds=float(values["total_processing_time_seconds"]),
        )
        for user_id, values in sorted(aggregates.items())
    ]

    return MetricsSummaryResponse(
        totals=totals,
        total_jobs=sum(item.job_count for item in totals),
        total_duration_seconds=sum(item.total_duration_seconds for item in totals),
        total_processing_time_seconds=sum(item.total_processing_time_seconds for item in totals),
    )


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
