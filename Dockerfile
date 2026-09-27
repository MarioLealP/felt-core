FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY static/ ./static/
COPY plugins/ ./plugins/
RUN uv sync --frozen --no-dev

EXPOSE 8420
CMD ["uv", "run", "felt-core-serve"]
