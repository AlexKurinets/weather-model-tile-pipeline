FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# The rasterio and eccodes wheels bundle GDAL and ecCodes but link the system libexpat.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

# Dependencies first, against an empty package, so a code change does not reinstall them.
COPY pyproject.toml ./
RUN mkdir -p src/wxtiles && touch src/wxtiles/__init__.py && pip install -e ".[dev]"

COPY src ./src
COPY config ./config
COPY web ./web
COPY tests ./tests

ENV WX_SOURCES_FILE=/app/config/sources.yaml WX_WEB_DIR=/app/web
CMD ["wxtiles", "--help"]
