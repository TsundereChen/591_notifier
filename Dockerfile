FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
