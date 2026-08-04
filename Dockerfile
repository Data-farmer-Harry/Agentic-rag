FROM nikolaik/python-nodejs:python3.13-nodejs20-slim@sha256:ef37897d6366a5e510782b6f708106c5e4e447975036d898295542647c155d6b AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TIKTOKEN_CACHE_DIR=/opt/hermesgraph/tiktoken-cache

WORKDIR /app
COPY requirements.runtime.lock ./
COPY assets/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790 \
    /opt/hermesgraph/tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url="${PIP_INDEX_URL}" -r requirements.runtime.lock \
    && python -c "import tiktoken; encoding = tiktoken.get_encoding('o200k_base'); assert encoding.name == 'o200k_base'"

COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install --no-deps . \
    && pip check

COPY docs/ ./docs/
COPY examples/ ./examples/
COPY prompts/ ./prompts/
COPY frontend/dist/ ./frontend/dist/

RUN useradd --create-home --uid 10001 hermesgraph \
    && mkdir -p /data \
    && chown -R hermesgraph:hermesgraph /data

USER hermesgraph
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
