"""Create the private Storage bucket and check upload/download/delete with the secret key.

Usage:
    python -m scripts.setup_storage
"""

import uuid

from app.core.aio import run
from app.core.config import get_app_config
from app.services.storage import StorageError, get_supabase_storage


async def main() -> None:
    storage = get_supabase_storage()
    if storage is None:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    cfg = get_app_config()
    try:
        created = await storage.ensure_bucket(cfg.upload.max_file_mb * 1024 * 1024)
        print(f"bucket '{cfg.storage.bucket}': {'created' if created else 'exists'}")

        path = f"healthcheck/{uuid.uuid4()}.txt"
        await storage.upload(path, "ทดสอบ".encode(), "text/plain; charset=utf-8")
        assert (await storage.download(path)).decode() == "ทดสอบ"
        url = await storage.create_signed_upload_url(path)
        await storage.delete([path])
        print(f"upload/download/delete OK, signed URL OK ({url.split('?')[0][:60]}...)")
    except StorageError as exc:
        hint = ""
        if exc.status_code in (400, 401, 403):
            hint = (
                "\n→ SUPABASE_SERVICE_KEY ต้องเป็น secret key (sb_secret_...) จาก"
                " Project Settings → API Keys ไม่ใช่ publishable key"
            )
        raise SystemExit(f"Storage error {exc.status_code}: {exc}{hint}") from None


if __name__ == "__main__":
    run(main())
