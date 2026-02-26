FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY ai_logger.py .

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python3 -c "import requests; requests.get('https://discord.com/api/v10/gateway', timeout=5)" || exit 1

CMD ["python3", "main.py"]
