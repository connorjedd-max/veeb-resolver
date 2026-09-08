FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VEEB_BGUTIL_BASE_URL=http://127.0.0.1:4416

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

RUN git clone --depth 1 --branch 1.3.2 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && sed -i 's/host: "::"/host: "127.0.0.1"/g; s/host: "0.0.0.0"/host: "127.0.0.1"/g; s/address \[::\]/address 127.0.0.1/g; s/on \[::\]/on 127.0.0.1/g; s/address 0.0.0.0/address 127.0.0.1/g' /opt/bgutil/server/src/main.ts \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

COPY veeb_resolver.py media_jobs.py extract_source.py /app/
COPY tests /app/tests
RUN python -m unittest discover -s /app/tests -v \
    && python -c "import veeb_resolver" \
    && pip check

EXPOSE 10000

CMD ["sh", "-c", "set -e; node /opt/bgutil/server/build/main.js --port 4416 & BG_PID=$!; trap 'kill $BG_PID 2>/dev/null || true' EXIT INT TERM; i=0; until curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; do i=$((i+1)); [ $i -ge 100 ] && { echo 'POT server failed'; exit 1; }; sleep 0.2; done; exec uvicorn veeb_resolver:app --host 0.0.0.0 --port ${PORT:-10000}"]
