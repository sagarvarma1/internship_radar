# Portable image: the same container runs the web app (default) or the poller:
#   docker run -v radar:/data -p 8000:8000 radar
#   docker run -v radar:/data radar radar poll --spread 300
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config.yaml registry.yaml ./
ENV PATH="/app/.venv/bin:$PATH" RADAR_DB=/data/radar.db PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "radar serve --host 0.0.0.0 --port $PORT"]
