# rag-kham-kham

RAG API สำหรับค้นหาและแต่งมุกขำๆ ภาษาไทย (FastAPI + Supabase + LLM ฟรี)

- ความต้องการ: [`USER-STORIES-v3.md`](USER-STORIES-v3.md)
- การออกแบบ: [`DESIGN.md`](DESIGN.md)

## สถานะ

เฟส 1 ขั้นที่ 1–3 (ตาม DESIGN.md หัวข้อ 13): โครงโปรเจกต์, schema + migration แรก, envelope มาตรฐาน, API Key auth, `/health`

| Endpoint | Auth | หมายเหตุ |
|---|---|---|
| `GET /api/v1/health` | — | สถานะ DB / Storage / LLM |
| `GET /api/v1/auth/me` | user, admin | ตรวจว่า key เป็นของใคร |
| `GET /docs` | — | Swagger UI |

## เริ่มต้นใช้งาน (ในเครื่อง)

```bash
# 1. สร้าง virtualenv และติดตั้ง (หรือใช้ `uv sync` ถ้ามี uv)
python -m venv .venv
.venv/Scripts/activate            # Windows (macOS/Linux: source .venv/bin/activate)
pip install -e . pytest pytest-asyncio httpx ruff

# 2. ตั้งค่า
cp .env.example .env              # ใส่ค่า ADMIN_BOOTSTRAP_KEY อย่างน้อย 1 ตัว

# 3. รัน
uvicorn app.main:app --reload     # เปิด http://localhost:8000/docs
```

ยังไม่ต้องมี Supabase ก็รันได้ `/health` จะแสดง `not_configured` และใช้ `ADMIN_BOOTSTRAP_KEY` เรียก endpoint ที่ต้องใช้ key ได้

### รันด้วย Docker (เหมือน production)

```bash
docker compose up --build         # http://localhost:8000/docs
```

## ตั้งค่า Supabase

1. สร้างโปรเจกต์ที่ supabase.com แล้วเปิดใช้ extension `vector` (Database → Extensions)
2. คัดลอก connection string จาก **Connect** มาใส่ใน `.env`
   - `DATABASE_URL` = Transaction pooler (port **6543**) ใช้ตอนรันแอป
   - `DATABASE_URL_DIRECT` = Session pooler (port **5432**) ใช้ตอนรัน migration
   - เปลี่ยน prefix เป็น `postgresql+psycopg://`
3. สร้างตาราง: `alembic upgrade head`
4. สร้าง admin key ถาวร: `python -m scripts.create_api_key --owner me --role admin`
5. ใส่ `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (ใช้ในขั้นอัปโหลดไฟล์)

## พัฒนา

```bash
pytest                 # test
ruff check . && ruff format .
alembic revision -m "..."   # สร้าง migration ใหม่
```

## โครงสร้าง

```
app/
  core/      config, envelope, error, auth, logging
  api/v1/    endpoint
  db/        engine + ORM models
  services/  (เฟสถัดไป) แปลงไฟล์, ค้นหา, LLM, pipeline ตอบคำถาม
alembic/     migration (0001 = schema ทั้งหมดใน DESIGN.md 4.1)
scripts/     สคริปต์ช่วย เช่น สร้าง API key
config.yaml  ค่าที่ปรับได้ (threshold, quota, โมเดล) — ไม่มี secret
```

## Deploy (Vercel)

Vercel build จาก `Dockerfile.vercel` (เนื้อหาต้องตรงกับ `Dockerfile`) เมื่อ push `master`
ต้องตั้ง Environment Variables ตาม `.env.example` ใน Vercel dashboard
