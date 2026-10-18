# Production image: non-root, no build tools, PostgreSQL driver included.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements-prod.txt
COPY steward ./steward
COPY web ./web
RUN useradd --create-home --uid 10001 steward && mkdir -p /app/data && chown steward /app/data
USER steward
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["uvicorn", "steward.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
