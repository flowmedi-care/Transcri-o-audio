import asyncio
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Optional

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

TOTAL_STEPS = 8


def _job_log(job_id: str, step: int, message: str, **extra: Any) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in extra.items()) if extra else ""
    detail = f"{message} {suffix}".strip()
    logger.info("[Transcribe] job=%s step=%s/%s %s", job_id, step, TOTAL_STEPS, detail)


@dataclass
class EnqueueResult:
    job_id: str
    status: JobStatus


@dataclass
class QueueStats:
    pending: int
    queued: int
    processing: int
    worker_running: bool


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
        self._loop_cycles = 0

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        await self._init_db()
        recovered = await self._recover_stale_jobs(reason="startup", recover_all_processing=True)
        if recovered:
            logger.warning(
                "[Transcribe] startup recovered %s stale processing job(s) back to queued",
                recovered,
            )
        self._ensure_worker_running()
        logger.info(
            "[Transcribe] queue started db=%s temp=%s supabase=%s whisper=%s",
            self.db_path,
            self.temp_dir,
            self.storage.enabled,
            self.settings.whisper_model,
        )

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def _ensure_worker_running(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return

        if self._worker_task is not None and self._worker_task.done():
            exc = self._worker_task.exception()
            if exc:
                logger.error("[Transcribe] worker died unexpectedly: %s", exc, exc_info=exc)

        self._worker_task = asyncio.create_task(self._worker_loop(), name="transcription-worker")
        logger.info("[Transcribe] worker task started")

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
        _job_log(
            job_id,
            1,
            "enqueue_received",
            user_id=user_id,
            source=source or "none",
            filename=original_filename,
            bytes=len(raw_bytes),
        )

        now = _utc_now_iso()
        input_path = self.temp_dir / f"{job_id}_input{Path(original_filename).suffix.lower()}"
        input_path.write_bytes(raw_bytes)

        try:
            duration_seconds = probe_duration_seconds(input_path)
            _job_log(job_id, 2, "duration_probed", seconds=round(duration_seconds, 2))
        except AudioValidationError:
            input_path.unlink(missing_ok=True)
            logger.error("[Transcribe] job=%s step=2/%s duration_probe_failed", job_id, TOTAL_STEPS)
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

        try:
            await self.storage.create_job(
                job_id=job_id,
                user_id=user_id,
                source=source,
                model=self.settings.whisper_model,
                status=JobStatus.QUEUED.value,
            )
            _job_log(job_id, 3, "queued", sqlite="ok", supabase="ok")
        except Exception as exc:
            # Job is already in SQLite — do not fail the HTTP request because Supabase is down.
            _job_log(job_id, 3, "queued", sqlite="ok", supabase="error", error=str(exc))
            logger.warning(
                "[Transcribe] job=%s Supabase create failed (job will still process locally): %s",
                job_id,
                exc,
            )

        self._wake_event.set()
        self._ensure_worker_running()
        return EnqueueResult(job_id=job_id, status=JobStatus.QUEUED)

    async def get_job(self, job_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def pending_count(self) -> int:
        stats = await self.get_stats()
        return stats.pending

    async def get_stats(self) -> QueueStats:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued', 'processing') GROUP BY status"
            )
            rows = await cursor.fetchall()

        counts = {status: int(count) for status, count in rows}
        queued = counts.get(JobStatus.QUEUED.value, 0)
        processing = counts.get(JobStatus.PROCESSING.value, 0)
        worker_running = self._worker_task is not None and not self._worker_task.done()
        return QueueStats(
            pending=queued + processing,
            queued=queued,
            processing=processing,
            worker_running=worker_running,
        )

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

    async def _recover_stale_jobs(
        self, *, reason: str, recover_all_processing: bool = False
    ) -> int:
        """Re-queue jobs stuck in processing (e.g. after OOM kill or crash)."""
        now = _utc_now_iso()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=self.settings.stale_job_minutes)
        ).isoformat()

        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                if recover_all_processing:
                    cursor = await db.execute(
                        """
                        UPDATE jobs
                        SET status = ?, updated_at = ?, error_message = NULL
                        WHERE status = ?
                        """,
                        (JobStatus.QUEUED.value, now, JobStatus.PROCESSING.value),
                    )
                else:
                    cursor = await db.execute(
                        """
                        UPDATE jobs
                        SET status = ?, updated_at = ?, error_message = NULL
                        WHERE status = ?
                          AND (
                            started_at IS NULL
                            OR started_at < ?
                            OR updated_at < ?
                          )
                        """,
                        (
                            JobStatus.QUEUED.value,
                            now,
                            JobStatus.PROCESSING.value,
                            cutoff,
                            cutoff,
                        ),
                    )
                await db.commit()
                return cursor.rowcount or 0

    async def _worker_loop(self) -> None:
        logger.info("[Transcribe] worker_loop running")
        while True:
            try:
                self._loop_cycles += 1
                if self._loop_cycles % 30 == 0:
                    recovered = await self._recover_stale_jobs(reason="periodic")
                    if recovered:
                        logger.warning(
                            "[Transcribe] periodic recovery re-queued %s stale job(s)",
                            recovered,
                        )

                job = await self._claim_next_job()
                if job is None:
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

                await self._process_job(job)
            except asyncio.CancelledError:
                logger.info("[Transcribe] worker_loop cancelled")
                raise
            except Exception:
                logger.exception("[Transcribe] worker_loop error — retrying in 2s")
                await asyncio.sleep(2)
                self._wake_event.set()

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
                _job_log(job["id"], 4, "worker_claimed", filename=job.get("original_filename"))
                return job

    async def _process_job(self, job: dict) -> None:
        job_id = job["id"]
        input_path = Path(job["input_path"])
        wav_path = self.temp_dir / f"{job_id}.wav"
        now = _utc_now_iso()

        if not input_path.exists():
            error_message = f"Input audio file missing: {input_path}"
            logger.error("[Transcribe] job=%s %s", job_id, error_message)
            await self._fail_job(job_id, error_message, now)
            return

        try:
            await self.storage.update_job(job_id, {"status": JobStatus.PROCESSING.value})
        except Exception as exc:
            logger.warning(
                "[Transcribe] job=%s Supabase processing update failed (continuing): %s",
                job_id,
                exc,
            )

        try:
            _job_log(job_id, 5, "converting_to_wav")
            convert_to_wav(input_path, wav_path)

            _job_log(job_id, 6, "whisper_start", model=self.settings.whisper_model)
            result = await asyncio.wait_for(
                asyncio.to_thread(transcribe_audio, wav_path, self.settings),
                timeout=self.settings.max_processing_seconds,
            )
            _job_log(
                job_id,
                7,
                "whisper_done",
                processing_time=round(result.processing_time_seconds, 2),
                text_len=len(result.text),
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
            try:
                await self.storage.update_job(job_id, supabase_fields)
            except Exception as exc:
                logger.warning(
                    "[Transcribe] job=%s Supabase completed update failed: %s",
                    job_id,
                    exc,
                )

            _job_log(job_id, 8, "completed")

        except Exception as exc:
            error_message = str(exc)
            logger.exception("[Transcribe] job=%s failed at processing: %s", job_id, error_message)
            await self._fail_job(job_id, error_message, now)

        finally:
            self._cleanup_paths(input_path, wav_path)
            self._wake_event.set()

    async def _fail_job(self, job_id: str, error_message: str, started_at: str) -> None:
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
                    _utc_now_iso(),
                    _utc_now_iso(),
                    job_id,
                ),
            )
            await db.commit()

        try:
            await self.storage.update_job(
                job_id,
                {"status": JobStatus.FAILED.value, "error_message": error_message},
            )
        except Exception as exc:
            logger.warning("[Transcribe] job=%s Supabase failed update error: %s", job_id, exc)

        _job_log(job_id, 8, "failed", error=error_message[:200])

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
