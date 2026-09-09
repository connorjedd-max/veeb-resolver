FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VEEB_BGUTIL_BASE_URL=http://127.0.0.1:4416
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*
ENV DENO_INSTALL=/usr/local DENO_DIR=/tmp/deno-cache
RUN curl -fsSL https://deno.land/install.sh | sh -s v2.8.1 && deno --version
WORKDIR /app
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN git clone --depth 1 --branch 2.0.0 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server && npm ci && npx tsc
COPY *.py /app/
COPY tests /app/tests
RUN python -m unittest discover -s /app/tests -v && python -c "import veeb_resolver" && pip check
EXPOSE 10000
ENTRYPOINT ["python", "/app/run_service.py"]
