FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --user .

# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 transcriber
USER transcriber
ENV PATH="/home/transcriber/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1

COPY --from=builder --chown=transcriber:transcriber /root/.local /home/transcriber/.local

WORKDIR /home/transcriber

# livekit-agents `dev` / `start` mode; start = production worker loop.
ENTRYPOINT ["meeting-transcriber"]
CMD ["start"]
