FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx influxdb-client

COPY collector.py .

CMD ["python", "-u", "collector.py"]
