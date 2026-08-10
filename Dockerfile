FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/data/config.yaml \
    CONFIG_TEMPLATE_PATH=/app/config.yaml \
    DATABASE_PATH=/data/listings.db

WORKDIR /app

RUN addgroup --system notifier \
    && adduser --system --ingroup notifier --home /app notifier \
    && mkdir -p /data \
    && chown notifier:notifier /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crawler.py main.py config_store.py notifier.py bot.py config.yaml ./

USER notifier
VOLUME ["/data"]

CMD ["python", "bot.py"]
