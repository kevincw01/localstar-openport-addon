FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends dosfstools mtools \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py run_addon.py addon_config.py management.py certificate_store.py telemetry.py mqtt_entities.py location.py supervisor_options.py sd_card_image.py ./
COPY web/ ./web/

EXPOSE 9001/tcp 41414/tcp 8099/tcp
CMD ["python3", "/app/run_addon.py"]
