FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx influxdb-client tzdata matplotlib

COPY collector.py .
COPY bath_detector.py .
COPY charge_detector.py .
COPY daily_report.py .
COPY rates.py .
COPY categories.json .

CMD ["python", "-u", "collector.py"]
