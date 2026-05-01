FROM python:3.9.19-slim-bookworm AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.9.19-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 trader

COPY --from=build /install /usr/local
WORKDIR /app
COPY --chown=trader:trader . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TZ=UTC

USER trader

HEALTHCHECK --interval=2m --timeout=10s --start-period=2m --retries=3 \
  CMD python -c "import os, sys, time; sys.exit(0 if os.path.exists('/app/data/heartbeat') and time.time() - os.path.getmtime('/app/data/heartbeat') < 600 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/deploy/run.py"]
