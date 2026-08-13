FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pin the bgutil provider server to the same version as the yt-dlp plugin.
RUN git clone --depth 1 --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip \
    && /venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY veeb_resolver.py /app/veeb_resolver.py

ENV PATH="/venv/bin:${PATH}"
ENV PORT=10000
ENV TOKEN_TTL=6

EXPOSE 10000

CMD ["sh", "-c", "uvicorn veeb_resolver:app --host 0.0.0.0 --port ${PORT:-10000}"]
