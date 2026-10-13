FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# Compte système dédié, sans privilèges
RUN useradd --system --uid 10001 --no-create-home portail \
    && mkdir -p /data && chown portail:portail /data

WORKDIR /srv/app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

USER portail
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# --preload : la base est initialisée une seule fois avant le fork des workers
CMD ["gunicorn", "--preload", "-w", "2", "--threads", "4", "-b", "0.0.0.0:8000", "app:app"]
