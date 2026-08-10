FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    CONFIG_PATH=/data/config.yaml \
    CONFIG_TEMPLATE_PATH=/app/config.yaml.example \
    DATABASE_PATH=/data/listings.db

WORKDIR /app

RUN addgroup --system notifier \
    && adduser --system --ingroup notifier --home /app notifier \
    && mkdir -p /data \
    && chown notifier:notifier /data

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config.yaml.example ./

USER notifier
VOLUME ["/data"]

CMD ["python", "-m", "rent591_notifier"]
