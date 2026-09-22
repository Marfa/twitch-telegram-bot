FROM python:3.13-slim

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

RUN mkdir -p /data/stream_preview && chown -R appuser:appuser /data

USER appuser

ENV DATABASE_PATH=/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request; b=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).read(); assert b==b'ok'" || exit 1

CMD ["python", "main.py"]
