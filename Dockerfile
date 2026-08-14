# 42 apply meeting watcher — pure HTTP, no browser.
FROM python:3.12-slim

# curl_cffi ships its own bundled libcurl-impersonate in the wheel; we only need
# CA certificates at runtime for TLS verification.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py .

# state/ (cookies, BOOKED sentinel, debug dumps) is a mounted volume.
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/state"]

CMD ["python", "watcher.py", "run"]
