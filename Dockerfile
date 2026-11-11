# Works on x86_64 and ARM (Raspberry Pi, Graviton, Apple silicon): tgcrypto is compiled in the build stage.
FROM python:3.15-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.15-slim
RUN useradd --create-home bot && mkdir -p /data /app && chown bot /data /app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl && rm -rf /wheels
COPY *.py ./
USER bot
ENV PYTHONUNBUFFERED=1 \
    SESSION_DIR=/data \
    DATABASE_URL=sqlite:////data/bot.db
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 CMD ["python", "healthcheck.py"]
VOLUME /data
CMD ["python", "bot.py"]
