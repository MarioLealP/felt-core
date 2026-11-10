# What runs is Meru/felt-core's Dockerfile (it builds this repo at a pinned commit); this one is for local builds.
FROM docker.io/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY static/ ./static/
COPY plugins/ ./plugins/
RUN uv sync --frozen --no-dev
# the semantic router's embedding model (brain/router.py) - baked in so
# felt-core never downloads anything at runtime
RUN uv run --no-sync python -c "from fastembed import TextEmbedding; \
TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/models')"

EXPOSE 8420
CMD ["uv", "run", "felt-core-serve"]
