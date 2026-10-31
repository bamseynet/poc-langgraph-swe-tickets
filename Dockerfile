# Builds the API and the UI into one image, served by FastAPI on port 8000.

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS app
# git clones the target checkout and runs `git apply --check` on proposed patches.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.6
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev
COPY examples/ examples/
COPY scripts/container-start.sh scripts/
COPY --from=web /web/dist web/dist
RUN useradd --uid 1000 --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["scripts/container-start.sh"]
