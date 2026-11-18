FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg git intel-opencl-icd libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv watchdog

WORKDIR /app
COPY examples/debian-docker/watcher.py /app/watcher.py

CMD ["python", "/app/watcher.py"]
