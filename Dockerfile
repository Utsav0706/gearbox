# Gearbox API image. CPU only: router v3 runs a small embedding model on CPU in ~20 ms.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.hf

WORKDIR /app

# CPU-only torch first: the default wheel bundles ~2 GB of CUDA that a CPU box never uses.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Bake the router's embedding model into the image, so the container starts without
# downloading anything.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

COPY config.yaml alembic.ini ./
COPY migrations ./migrations
COPY prompts ./prompts
COPY models ./models

RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

ENV HOST=0.0.0.0
EXPOSE 8002
CMD ["python", "-m", "gearbox.service"]
