FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py run_addon.py ./

EXPOSE 9001/tcp 41414/tcp
CMD ["python3", "/app/run_addon.py"]
