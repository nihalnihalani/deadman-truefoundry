# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user
RUN addgroup --system deadman && adduser --system --ingroup deadman deadman

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source. .dockerignore excludes .git, .deadman_state/, tests/,
# docs/, __pycache__, .env, *.pyc, .venv so the image stays minimal.
COPY --chown=deadman:deadman . .

RUN mkdir -p /app/.deadman_state && chown -R deadman:deadman /app/.deadman_state

# Drop to non-root
USER deadman

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["uvicorn", "deadman.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
