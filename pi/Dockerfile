FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx influxdb-client tzdata matplotlib

COPY collector.py .
COPY bath_detector.py .
COPY charge_detector.py .
COPY daily_report.py .
COPY report_baseline.py .
COPY rates.py .
COPY categories.json .
# backfill_rollups.py reads the task .flux files directly — it does not
# reimplement the pipeline, so both must ship together.
COPY backfill_rollups.py .
COPY influx_tasks/ ./influx_tasks/

CMD ["python", "-u", "collector.py"]
