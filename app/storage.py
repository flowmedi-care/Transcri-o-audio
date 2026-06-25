import logging
from typing import Any, Optional

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class SupabaseStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.supabase_url.rstrip("/")
        self.headers = {
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    @property
    def enabled(self) -> bool:
        return self.settings.supabase_enabled and self.settings.save_metrics

    async def create_job(
        self,
        job_id: str,
        user_id: str,
        source: Optional[str],
        model: str,
        status: str,
    ) -> None:
        if not self.enabled:
            logger.debug("[Transcribe] supabase disabled — skip create job=%s", job_id)
            return

        payload = {
            "id": job_id,
            "user_id": user_id,
            "source": source,
            "model": model,
            "status": status,
        }
        logger.info("[Transcribe] supabase create job=%s status=%s user_id=%s", job_id, status, user_id)
        await self._post("transcription_jobs", payload)

    async def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        if not self.enabled:
            return

        if not self.settings.save_transcript and "transcript" in fields:
            fields = {key: value for key, value in fields.items() if key != "transcript"}

        logger.info(
            "[Transcribe] supabase update job=%s fields=%s",
            job_id,
            ",".join(sorted(fields.keys())),
        )
        await self._patch(f"transcription_jobs?id=eq.{job_id}", fields)

    async def get_metrics_summary(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        params = {
            "select": "user_id,duration_seconds,processing_time_seconds,status",
            "status": "eq.completed",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.base_url}/rest/v1/transcription_jobs",
                headers=self.headers,
                params=params,
            )
            response.raise_for_status()
            return response.json()

    async def _post(self, table: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/rest/v1/{table}",
                headers=self.headers,
                json=payload,
            )
            if response.status_code >= 400:
                logger.error("Supabase insert failed: %s %s", response.status_code, response.text)
                response.raise_for_status()

    async def _patch(self, path: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.patch(
                f"{self.base_url}/rest/v1/{path}",
                headers=self.headers,
                json=payload,
            )
            if response.status_code >= 400:
                logger.error("Supabase update failed: %s %s", response.status_code, response.text)
                response.raise_for_status()
