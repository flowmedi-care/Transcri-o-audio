import asyncio
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Optional

import aiosqlite

from app.audio_utils import (
    AudioValidationError,
    convert_to_wav,
    probe_duration_seconds,
    validate_extension,
)
from app.config import Settings, get_settings
from app.models import JobStatus
from app.storage import SupabaseStorage
from app.transcribe import transcribe_audio

logger = logging.getLogger(__name__)


@dataclass
class EnqueueResult:
    job_id: str
    status: JobStatus


class RateLimitError(Exception):
    pass


class JobQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.queue_db_path
        self.temp_dir = settings.temp_dir
        self.storage = SupabaseStorage(settings)
        self._worker_task: Optional[asyncio.Task] = None
        self._wake_event = asyncio.Event()
        self._rate_buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        await self._init_db()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop(), name="transcription-worker")

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def enqueue(
        self,
        *,
        user_id: str,
        source: Optional[str],
        original_filename: str,
        raw_bytes: bytes,
    ) -> EnqueueResult:
        self._check_rate_limit(user_id)
        validate_extension(original_filename)

        if len(raw_bytes) > self.settings.max_file_size_bytes:
            raise AudioValidationError(
                f"File exceeds maximum size of {self.settings.max_file_size_mb} MB"
            )

        job_id = str(uuid.uuid4())
        now = _utc_now_iso()
        input_path = self.temp_dir / f"{job_id}_input{Path(original_filename).suffix.lower()}"
        input_path.write_bytes(raw_bytes)

        try:
            duration_seconds = probe_duration_seconds(input_path)
        except AudioValidationError:
            input_path.unlink(missing_ok=True)
            raise

        if duration_seconds > self.settings.max_duration_seconds:
            input_path.unlink(missing_ok=True)
            raise AudioValidationError(
                f"Audio exceeds maximum duration of {self.settings.max_duration_minutes} minutes"
            )

        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO jobs (
                        id, user_id, source, status, input_path, original_filename,
                        duration_seconds, model, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        user_id,
                        source,
                        JobStatus.QUEUED.value,
                        str(input_path),
                        original_filename,
                        duration_seconds,
                        self.settings.whisper_model,
                        now,
                        now,
                    ),
                )
                await db.commit()

        await self.storage.create_job(
            job_id=job_id,
            user_id=user_id,
            source=source,
            model=self.settings.whisper_model,
            status=JobStatus.QUEUED.value,
        )

        self._wake_event.set()
        return EnqueueResult(job_id=job_id, status=JobStatus.QUEUED)

    async def get_job(self, job_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'processing')"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    def _check_rate_limit(self, user_id: str) -> None:
        now = monotonic()
        window = 60.0
        bucket = self._rate_buckets[user_id]

        while bucket and now - bucket[0] > window:
            bucket.popleft()

        if len(bucket) >= self.settings.max_jobs_per_minute:
            raise RateLimitError(
                f"Rate limit exceeded: max {self.settings.max_jobs_per_minute} jobs per minute for this user"
            )

        bucket.append(now)

    async def _init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source TEXT,
                    status TEXT NOT NULL,
                    input_path TEXT NOT NULL,
                    wav_path TEXT,
                    original_filename TEXT NOT NULL,
                    duration_seconds REAL,
                    processing_time_seconds REAL,
                    model TEXT NOT NULL,
                    transcript TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at)"
            )
            await db.commit()

    async def _worker_loop(self) -> None:
        while True:
            job = await self._claim_next_job()
            if job is None:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            await self._process_job(job)

    async def _claim_next_job(self) -> Optional[dict]:
        now = _utc_now_iso()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (JobStatus.QUEUED.value,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None

                job = dict(row)
                await db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        JobStatus.PROCESSING.value,
                        now,
                        now,
                        job["id"],
                        JobStatus.QUEUED.value,
                    ),
                )
                await db.commit()
                return job

    async def _process_job(self, job: dict) -> None:
        job_id = job["id"]
        input_path = Path(job["input_path"])
        wav_path = self.temp_dir / f"{job_id}.wav"
        now = _utc_now_iso()

        await self.storage.update_job(job_id, {"status": JobStatus.PROCESSING.value})

        try:
            convert_to_wav(input_path, wav_path)
            result = await asyncio.wait_for(
                asyncio.to_thread(transcribe_audio, wav_path, self.settings),
                timeout=self.settings.max_processing_seconds,
            )

            update_fields = {
                "status": JobStatus.COMPLETED.value,
                "wav_path": str(wav_path),
                "duration_seconds": result.duration_seconds,
                "processing_time_seconds": result.processing_time_seconds,
                "transcript": result.text,
                "updated_at": _utc_now_iso(),
                "completed_at": _utc_now_iso(),
            }

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, wav_path = ?, duration_seconds = ?,
                        processing_time_seconds = ?, transcript = ?,
                        updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        update_fields["status"],
                        update_fields["wav_path"],
                        update_fields["duration_seconds"],
                        update_fields["processing_time_seconds"],
                        update_fields["transcript"],
                        update_fields["updated_at"],
                        update_fields["completed_at"],
                        job_id,
                    ),
                )
                await db.commit()

            supabase_fields = {
                "status": JobStatus.COMPLETED.value,
                "duration_seconds": result.duration_seconds,
                "processing_time_seconds": result.processing_time_seconds,
            }
            if self.settings.save_transcript:
                supabase_fields["transcript"] = result.text
            await self.storage.update_job(job_id, supabase_fields)

        except Exception as exc:
            error_message = str(exc)
            logger.exception("Job %s failed: %s", job_id, error_message)

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error_message = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        error_message,
                        now,
                        _utc_now_iso(),
                        job_id,
                    ),
                )
                await db.commit()

            await self.storage.update_job(
                job_id,
                {"status": JobStatus.FAILED.value, "error_message": error_message},
            )

        finally:
            self._cleanup_paths(input_path, wav_path)
            self._wake_event.set()

    def _cleanup_paths(self, input_path: Path, wav_path: Path) -> None:
        if not self.settings.save_audio:
            input_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
        elif wav_path.exists():
            wav_path.unlink(missing_ok=True)


_queue: Optional[JobQueue] = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue(get_settings())
    return _queue


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
