# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_DIR=/app/state

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY assistant_bot ./assistant_bot

# /app/state holds the Telegram polling offset; pre-create it owned by appuser
# so a fresh named volume inherits ownership on first mount.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/state \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "-m", "assistant_bot"]
