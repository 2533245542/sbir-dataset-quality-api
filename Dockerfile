# Generated deployment template for Render.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY quality_api.py .
COPY api.db.gz .
RUN gzip -d api.db.gz

ENV DB_PATH=/app/api.db
ENV PYTHONUNBUFFERED=1

EXPOSE 10000

CMD ["sh", "-c", "exec uvicorn quality_api:app --host 0.0.0.0 --port ${PORT:-10000}"]
