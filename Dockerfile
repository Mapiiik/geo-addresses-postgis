FROM osgeo/gdal:ubuntu-small-latest

# Stream Python output straight to stdout so `docker logs` shows live progress.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so they're cached separately from the source.
COPY importer/requirements.txt importer/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r importer/requirements.txt

COPY importer/ ./importer/

# Default = scheduler daemon (runs imports monthly).
# Override `command:` in compose for a one-shot import, e.g.:
#   docker compose run --rm addresses_importer python3 -m importer.import_cz_csv
CMD ["python3", "-m", "importer.scheduler"]
