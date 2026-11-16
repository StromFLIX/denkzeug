# No application dependencies, credentials, data volumes, or backend access.
FROM python:3.13.18-slim-bookworm
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home sandbox
WORKDIR /sandbox
COPY services/backend/nautionette_backend/app_sandbox.py ./app_sandbox.py
COPY services/backend/nautionette_backend/data/app-sandbox.js ./data/app-sandbox.js
USER sandbox
EXPOSE 8092
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8092/healthz',timeout=4).read()"
CMD ["python", "app_sandbox.py"]
