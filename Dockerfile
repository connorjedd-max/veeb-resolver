FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv git ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Native Deno is used by yt-dlp for YouTube EJS challenge solving.
ENV DENO_INSTALL=/usr/local \
    DENO_DIR=/tmp/deno-cache
RUN curl -fsSL https://deno.land/install.sh | sh -s v2.8.1 \
    && deno --version

WORKDIR /app

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN git clone --depth 1 --branch 1.3.1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

COPY veeb_resolver.py /app/veeb_resolver.py

EXPOSE 10000

CMD ["sh", "-c", "deno eval '1+1' >/dev/null 2>&1 || true; node /opt/bgutil/server/build/main.js --port 4416 & until curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; do sleep 0.2; done; echo POT server ready before app startup; exec uvicorn veeb_resolver:app --host 0.0.0.0 --port ${PORT:-10000}"]
