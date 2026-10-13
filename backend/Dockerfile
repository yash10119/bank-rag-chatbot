FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding models into the image so containers start offline-ready.
RUN python -c "from fastembed import TextEmbedding, SparseTextEmbedding; \
TextEmbedding('BAAI/bge-small-en-v1.5'); SparseTextEmbedding('Qdrant/bm25')"

COPY app ./app
RUN useradd -m appuser && mkdir -p /data/uploads && chown -R appuser /data /opt/fastembed
USER appuser
