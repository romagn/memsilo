# Stage 1: export requirements from Poetry
FROM python:3.12-slim AS deps
RUN pip install --no-cache-dir poetry poetry-plugin-export
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --without-hashes -o requirements.txt

# Stage 2: runtime image
FROM python:3.12-slim
LABEL org.opencontainers.image.title="memsilo" \
      org.opencontainers.image.description="Multi-user memory MCP server with per-user access" \
      org.opencontainers.image.source="https://github.com/romagn/memsilo"

# A fixed, non-root uid/gid. users.json is replaced atomically, so its owner is
# whoever ran the writer; a fixed id keeps ./data ownership predictable across
# rebuilds, and the CLI must run as this same user (never as root).
RUN groupadd --gid 10001 memsilo \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin memsilo

WORKDIR /app
COPY --from=deps /app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py users.py memory.py mcp_server.py ./
COPY scripts/ ./scripts/

# Application code stays root-owned (read-only to the runtime user). Only the
# data directory (a bind mount in docker-compose.yml, which must be writable by
# uid 10001) and the embedding-model cache belong to the memsilo user.
RUN mkdir -p /app/data /opt/fastembed \
 && chown memsilo:memsilo /app/data /opt/fastembed
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
# onnxruntime (used by fastembed) starts Microsoft telemetry when it is imported
# unless ORT_DISABLE_TELEMETRY=1 is set first; the server and CLI set it too,
# this also covers the build step below. Hugging Face Hub telemetry: off.
ENV ORT_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_TELEMETRY=1
USER memsilo

# Bake in both models the server uses: the dense embedder and the BM25 encoder
# Mem0's hybrid search loads (fastembed SparseTextEmbedding 'Qdrant/bm25').
# Not best effort: the runtime below is offline, and Mem0 silently disables
# BM25 search when the encoder cannot load, so a build without them must fail.
RUN python -c "from fastembed import SparseTextEmbedding, TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5'); SparseTextEmbedding('Qdrant/bm25')"

# No model downloads at runtime: fastembed and huggingface_hub use the cache
# above only, so recreating the container contacts nobody.
ENV HF_HUB_OFFLINE=1

EXPOSE 8100

CMD ["python", "mcp_server.py"]
