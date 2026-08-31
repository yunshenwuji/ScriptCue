# ScriptCue 服务端镜像
# 构建上下文为仓库根目录（需打包跨组件的 controller/），在根目录执行：
#   docker build -t scriptcue-server .
FROM python:3.13-slim

# 容器内日志即时可见（docker logs 不被缓冲）、不生成字节码文件
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖，利用层缓存（源码变更不触发重装）
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server/server/ ./server/
COPY controller/ ./controller/

# 审计日志等运行时数据目录，部署时挂载卷保留
ENV SC_DATA_DIR=/app/data

# 以非特权用户运行；数据目录归其所有（命名卷首次初始化会继承该属主）
RUN useradd -r -s /usr/sbin/nologin scriptcue \
    && mkdir -p /app/data \
    && chown -R scriptcue:scriptcue /app/data
USER scriptcue

VOLUME /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
