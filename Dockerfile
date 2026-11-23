# Used for local runs (docker compose) AND copied verbatim to Dockerfile.vercel for Vercel.
# Keep the two files identical.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=80

# Tesseract + Thai language data for OCR (US-C02). Can be disabled via config.yaml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tha \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN uv pip install --system --no-cache .

COPY config.yaml alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

EXPOSE 80
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
