# Friends API image
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY dashboard ./dashboard
COPY docker/entrypoint.sh /entrypoint.sh

# Non-root user; media dir owned by it so the named volume inherits the ownership.
RUN useradd --uid 10001 --create-home friends \
    && mkdir -p /data/media/avatars && chown -R friends:friends /data/media \
    && chmod +x /entrypoint.sh
USER friends

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
ENTRYPOINT ["/entrypoint.sh"]
