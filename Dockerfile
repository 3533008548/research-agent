ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/app/runtime/derived/home \
    XDG_CACHE_HOME=/app/runtime/derived/cache \
    HF_HOME=/app/runtime/derived/cache/huggingface

WORKDIR /app

# ChromaDB's ONNX runtime requires libgomp.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN mkdir -p /app/runtime/derived/home/.cache /app/runtime/derived/cache \
    && chown -R app:app /app

USER app

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:7860/api/v1/health', timeout=3).status == 200" || exit 1

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "7860"]
