"""Supabase Storage over its REST API (no supabase-py dependency).

Needs the project's **secret** key (`sb_secret_...` or the legacy `service_role` JWT): the
bucket is private and the app bypasses Storage policies. Never expose this key to clients;
clients only ever get short-lived signed upload URLs.
"""

import logging
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_app_config, get_settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)


class StorageError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ObjectNotFound(StorageError):
    pass


class Storage(Protocol):
    async def upload(self, path: str, data: bytes, content_type: str) -> None: ...
    async def download(self, path: str) -> bytes: ...
    async def delete(self, paths: list[str]) -> None: ...
    async def create_signed_upload_url(self, path: str) -> str: ...


class SupabaseStorage:
    def __init__(self, base_url: str, key: str, bucket: str, timeout: float = 60.0) -> None:
        self._base = base_url.rstrip("/") + "/storage/v1"
        self._bucket = bucket
        self._auth = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._timeout = timeout

    def _object_url(self, path: str, prefix: str = "object") -> str:
        return f"{self._base}/{prefix}/{self._bucket}/{quote(path)}"

    async def _request(
        self, method: str, url: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method, url, headers={**self._auth, **(headers or {})}, **kwargs
            )
        if resp.status_code >= 400:
            body = resp.text[:300]
            # Storage reports a missing object as 404, or 400 with {"statusCode":"404"}.
            if resp.status_code == 404 or '"404"' in body:
                raise ObjectNotFound(body, resp.status_code)
            logger.error("Storage %s %s -> %s %s", method, url, resp.status_code, body)
            raise StorageError(body, resp.status_code)
        return resp

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        await self._request(
            "POST",
            self._object_url(path),
            headers={"Content-Type": content_type, "x-upsert": "true"},
            content=data,
        )

    async def download(self, path: str) -> bytes:
        return (await self._request("GET", self._object_url(path, "object/authenticated"))).content

    async def delete(self, paths: list[str]) -> None:
        if paths:
            url = f"{self._base}/object/{self._bucket}"
            await self._request("DELETE", url, json={"prefixes": paths})

    async def create_signed_upload_url(self, path: str) -> str:
        """URL the client PUTs the file to (valid 2 hours). Overwrites an existing object."""
        resp = await self._request(
            "POST", self._object_url(path, "object/upload/sign"), headers={"x-upsert": "true"}
        )
        return self._base + resp.json()["url"]

    async def ensure_bucket(self, file_size_limit: int) -> bool:
        """Create the private bucket if missing. Returns True when it was created."""
        try:
            await self._request("GET", f"{self._base}/bucket/{self._bucket}")
            return False
        except ObjectNotFound:
            pass
        await self._request(
            "POST",
            f"{self._base}/bucket",
            json={
                "id": self._bucket,
                "name": self._bucket,
                "public": False,
                "file_size_limit": file_size_limit,
            },
        )
        return True


@lru_cache
def get_supabase_storage() -> SupabaseStorage | None:
    s = get_settings()
    if not (s.supabase_url and s.supabase_service_key):
        return None
    return SupabaseStorage(s.supabase_url, s.supabase_service_key, get_app_config().storage.bucket)


def get_storage() -> Storage:
    """FastAPI dependency (overridden in tests)."""
    storage = get_supabase_storage()
    if storage is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "ยังไม่ได้ตั้งค่า SUPABASE_URL / SUPABASE_SERVICE_KEY",
            status_code=500,
        )
    return storage
