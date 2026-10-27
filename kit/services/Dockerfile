# The simulated clinic API (system of record). Build from the repo root:
#   docker build -f services/Dockerfile -t clinic-api .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.52.1 pydantic==2.12.5 python-dotenv==1.2.4 tzdata==2026.5
COPY services/ ./services/

EXPOSE 8101
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8101/health')"
CMD ["uvicorn", "services.clinic_api:app", "--host", "0.0.0.0", "--port", "8101"]
