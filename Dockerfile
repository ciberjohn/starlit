# Starlit — crochet pattern to symbol chart.
FROM python:3.12-slim

# poppler-utils provides pdftotext (a hard requirement of the engine); pdftoppm/pdfinfo are used
# by the A4 test. fonts-dejavu-core gives the chart its labels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/starlit
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY app ./app
COPY engine ./engine
COPY sample ./sample
COPY tests ./tests
COPY README.md LICENSE ./

# State (app.db, charts/, uploads/) lives in a volume so it survives a container restart.
ENV STARLIT_DATA=/data \
    STARLIT_HOST=0.0.0.0 \
    STARLIT_PORT=6090 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 6090

# One worker, several threads: the on-screen progress (/progress polling) needs the server to
# answer a poll *during* a conversion, so the worker must be threaded.
CMD ["gunicorn", "--bind", "0.0.0.0:6090", "--workers", "1", "--threads", "8", "app.app:app"]
