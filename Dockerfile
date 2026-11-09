# syntax=docker/dockerfile:1.7
FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app

COPY requirements.txt /app/requirements.txt
# Optional public CA bundle for managed environments with a TLS inspection proxy.
# Without a secret this is a regular pip install using the image's CA store.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app/media /app/staticfiles /app/incoming \
    && chown -R app:app /app
COPY --chown=app:app . /app
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod 0755 /app/docker-entrypoint.sh
USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "180", "--access-logfile", "-", "--error-logfile", "-"]
