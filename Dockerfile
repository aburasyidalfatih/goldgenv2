FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    # Behind Dokploy's Traefik: trust its X-Forwarded-Proto/-For so HTTPS is detected
    # (session cookie gets the Secure flag) and logs show the visitor's IP.
    FORWARDED_ALLOW_IPS=* \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No apt-get: every dependency ships as a prebuilt manylinux wheel (Pillow bundles
# libjpeg/zlib/freetype), so no compiler or system libraries are needed. Skipping
# apt also keeps builds working when the Debian mirrors are unreachable from the VPS.
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# Copy source code
COPY . .

# Ensure storage and data directories exist
RUN mkdir -p /app/data /app/storage/generated_images /app/backups

# Expose server port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# Exactly ONE worker: the autopilot scheduler runs inside the app process, so more
# workers would post every scheduled item several times.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers"]
