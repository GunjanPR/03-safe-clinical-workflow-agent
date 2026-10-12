FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,otlp]"
COPY scripts ./scripts
# The store is mounted at /app/data (built on the host with `make data`).
EXPOSE 8020
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8020/health')"
CMD ["uvicorn", "clinagent.api:app", "--host", "0.0.0.0", "--port", "8020"]
