# syntax=docker/dockerfile:1
# ATYA v2: one image runs receiver, processor and sender (python -m atya).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY atya ./atya
COPY schema ./schema
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "-m", "atya"]
