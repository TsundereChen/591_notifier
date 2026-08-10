FROM python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6

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
