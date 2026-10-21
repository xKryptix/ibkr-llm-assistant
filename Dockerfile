FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY main.py mcp_client.py mcp_server.py settings.example.toml ./

RUN useradd -m appuser && mkdir -p data logs && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py", "--price-survey"]
