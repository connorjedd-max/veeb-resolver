FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VEEB_BGUTIL_BASE_URL=http://127.0.0.1:4416 \
    VEEB_YOUTUBEJS_BASE_URL=http://127.0.0.1:4417 \
    VEEB_MWEB_CLIENT_VERSION=2.20260708.05.00

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv git ca-certificates ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local \
    DENO_DIR=/tmp/deno-cache
RUN curl -fsSL https://deno.land/install.sh | sh -s v2.8.1 \
    && deno --version

WORKDIR /app
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY package.json /app/package.json
RUN npm install --omit=dev --no-audit --no-fund

RUN git clone --depth 1 --branch 1.3.1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && sed -i 's/host: "::"/host: "127.0.0.1"/g; s/host: "0.0.0.0"/host: "127.0.0.1"/g; s/address \[::\]/address 127.0.0.1/g; s/on \[::\]/on 127.0.0.1/g; s/address 0.0.0.0/address 127.0.0.1/g' /opt/bgutil/server/src/main.ts \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

COPY veeb_resolver.py /app/veeb_resolver.py
COPY veeb_innertube_helper.mjs /app/veeb_innertube_helper.mjs

EXPOSE 10000

CMD ["sh", "-c", "set -e; deno eval '1+1' >/dev/null 2>&1 || true; node /opt/bgutil/server/build/main.js --port 4416 & BG_PID=$!; YTJS_PID=; trap 'kill $BG_PID ${YTJS_PID:-} 2>/dev/null || true' EXIT INT TERM; i=0; until curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; do i=$((i+1)); [ $i -ge 450 ] && { echo 'POT server failed to become ready after 90s'; exit 1; }; sleep 0.2; done; echo 'POT server ready on loopback before YouTube.js startup'; node /app/veeb_innertube_helper.mjs & YTJS_PID=$!; i=0; until curl -fsS http://127.0.0.1:4417/health >/dev/null 2>&1; do i=$((i+1)); [ $i -ge 300 ] && { echo 'YouTube.js helper not warm after 60s; Uvicorn will retain yt-dlp fallback'; break; }; sleep 0.2; done; exec uvicorn veeb_resolver:app --host 0.0.0.0 --port ${PORT:-10000}"]
