FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
COPY check_kit.py .
RUN pip install --no-cache-dir -r requirements.txt
COPY actions/ ./actions/
COPY agent/*.py ./agent/
COPY app/ ./app/
COPY core/ ./core/
COPY integrations/ ./integrations/
COPY services/ ./services/
COPY ui/web/ ./ui/web/
COPY voice/ ./voice/

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
