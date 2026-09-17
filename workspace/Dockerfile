# 零第三方依赖：应用仅使用 Python 3.11 标准库（http.server + sqlite3），
# 因此镜像里不需要 pip install，构建快、攻击面小。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    CONTRACT_REGISTRY_DB=/data/registry.db

WORKDIR /app

# 非 root 运行；数据目录预先授权。
RUN useradd --create-home --uid 10001 appuser && mkdir -p /data \
    && chown -R appuser:appuser /data /app

COPY --chown=appuser:appuser app/ ./app/

USER appuser
EXPOSE 8080

# 容器级健康检查直接命中 /health（内置，无外部依赖）。
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import json,os,urllib.request; \
port=os.environ.get('PORT','8080'); \
r=urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2); \
assert json.load(r)['status']=='ok'" || exit 1

CMD ["python3", "-m", "app"]
