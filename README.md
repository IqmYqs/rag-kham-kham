# rag-kham-kham

RAG API สำหรับค้นหาและแต่งมุกขำๆ ภาษาไทย (FastAPI + Supabase + LLM ฟรี)

- ความต้องการ: [`USER-STORIES-v3.md`](USER-STORIES-v3.md)
- การออกแบบ: [`DESIGN.md`](DESIGN.md)

## สถานะ

เฟส 1 ขั้นที่ 1–8 (ตาม DESIGN.md หัวข้อ 13): โครงโปรเจกต์, auth, อัปโหลด + Storage + คิวแปลงไฟล์, MarkItDown + ลบข้อมูลส่วนบุคคล + ตรวจ/อนุมัติ, chunk + full-text + embedding + แท็ก, **hybrid search + `/ask` (ส่งตรง → .md → AI) + `/random`** — ถัดไป: deploy Vercel และชุดคำถามทดสอบ (เฟส 2: streaming, cache, โควตา, บทสนทนา)

| Endpoint | Auth | หมายเหตุ |
|---|---|---|
| `GET /api/v1/health` | — | สถานะ DB / Storage / LLM |
| `GET /api/v1/auth/me` | user, admin | ตรวจว่า key เป็นของใคร |
| `POST /api/v1/ask` | user, admin | ถามหามุก `{"message": "รองเท้า", "mode": "auto" \| "ai_only" \| "library_only"}` |
| `GET /api/v1/random` | user, admin | สุ่มมุกจากคลัง (`?category=` ได้) ใช้ 0 token |
| `POST /api/v1/files` | admin | อัปโหลด multipart หลายไฟล์ (≤ 4 MB/ไฟล์) |
| `POST /api/v1/files/uploads` | admin | ขอ signed URL (≤ 20 MB) → `PUT` ไฟล์ไปที่ `upload_url` |
| `POST /api/v1/files/complete` | admin | แจ้งว่าอัปโหลดเสร็จ → เข้าคิวแปลง |
| `GET /api/v1/files`, `GET /api/v1/files/{id}` | admin | รายการ/สถานะ (กู้งานค้างอัตโนมัติ) |
| `DELETE /api/v1/files/{id}` | admin | ลบไฟล์ + .md + chunk + cache |
| `GET` / `PUT /api/v1/files/{id}/markdown` | admin | ดู / แก้ .md (แก้ไฟล์ที่ approved แล้ว → กลับเป็น pending_review) |
| `POST /api/v1/files/{id}/approve`, `/reject` | admin | อนุมัติ (แบ่ง chunk + full-text ทันที, embedding/แท็กทำเบื้องหลัง ดูความคืบหน้าที่ `index` ใน `GET /files/{id}`) / ปฏิเสธ |
| `POST /api/v1/files/{id}/reprocess` | admin | แปลงใหม่ `{"method": "default" \| "llm"}` |
| `GET /api/v1/files/{id}/revisions` | admin | ประวัติการแก้ .md (มี diff) |
| `GET /api/v1/internal/cron` | Bearer `CRON_SECRET` | Vercel Cron วันละครั้ง (`vercel.json`) |
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

> Windows: psycopg แบบ async ใช้ event loop ปกติของ Windows ไม่ได้ ถ้ารันโดยไม่มี `--reload` ให้เพิ่ม `--loop app.core.aio:uvicorn_loop_factory` (สคริปต์ใน `scripts/` จัดการให้แล้ว)

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
   - ถ้ารหัสผ่านมีอักขระพิเศษ ต้อง encode ก่อน เช่น `@` → `%40`, `#` → `%23`, `/` → `%2F`
3. สร้างตาราง: `alembic upgrade head`
4. สร้าง admin key ถาวร: `python -m scripts.create_api_key --owner me --role admin`
5. ใส่ `SUPABASE_URL` และ `SUPABASE_SERVICE_KEY` = **secret key** (`sb_secret_...`) จาก Project Settings → API Keys (ไม่ใช่ publishable key)
6. สร้าง bucket และทดสอบ Storage: `python -m scripts.setup_storage`

> โควตา embedding ฟรีของ Gemini = 100 ข้อความ/นาที ไฟล์ใหญ่จะทยอยทำ embedding เบื้องหลัง (ค้นด้วยคำได้ทันที ค้นด้วยความหมายได้เมื่อ `index.embedded == index.chunks`)

## พัฒนา

```bash
pytest                 # test (ไม่เรียก API จริง)
RKK_INTEGRATION=1 pytest tests/test_*_integration.py   # ทดสอบกับ DB จริง (ไฟล์ชื่อ __test__* แล้วลบทิ้ง, ช้าเพราะ DB อยู่ Sydney)
python -m scripts.ingest samples/*.txt      # นำไฟล์เข้าคลังจริง + approve
python -m scripts.ask "รองเท้า" "วันนี้เหนื่อยจัง"   # ถามคลังจริงจาก command line
python -m scripts.llm_smoke   # ทดสอบ LLM + embedding ภาษาไทยกับ provider จริง (ใช้โควตาฟรีเล็กน้อย)
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
