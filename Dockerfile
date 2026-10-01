FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    # Behind Dokploy's Traefik: trust its X-Forwarded-Proto/-For so HTTPS is detected
    # (session cookie gets the Secure flag) and logs show the visitor's IP.
    FORWARDED_ALLOW_IPS=*

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Ensure storage and data directories exist
RUN mkdir -p /app/data /app/storage/generated_images

# Expose server port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Exactly ONE worker: the autopilot scheduler runs inside the app process, so more
# workers would post every scheduled item several times.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers"]
