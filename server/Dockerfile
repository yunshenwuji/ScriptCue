# ScriptCue 服务端镜像
# 构建需在仓库根目录执行：
#   docker build -f server/Dockerfile -t scriptcue-server .
FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/server/ ./server/
COPY controller/ ./controller/

# 审计日志等运行时数据目录，部署时挂载卷保留
ENV SC_DATA_DIR=/app/data
VOLUME /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
