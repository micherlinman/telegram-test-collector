FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

# Zeitzonendaten, damit Uhrzeiten und "heute/gestern" deutscher Zeit entsprechen
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY collector.py db.py web.py start.sh ./
COPY static ./static

# Session, Logs und Originalbilder liegen im Volume /data
ENV TELEGRAM_SESSION=/data/collector \
    MESSAGE_LOG_FILE=/data/messages.jsonl \
    TEXT_LOG_FILE=/data/messages.log \
    MEDIA_DIR=/data/media
VOLUME /data

EXPOSE 8000
# Standard: beides zusammen; docker-compose.yml startet sie als getrennte Dienste
CMD ["./start.sh"]
