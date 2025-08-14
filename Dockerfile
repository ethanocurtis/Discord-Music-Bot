# docker/Dockerfile for utila-music-bot
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Create user
RUN useradd -m -u 10001 appuser
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY . /app

USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "bot.py"]
