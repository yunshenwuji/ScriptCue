# ScriptCue 服务端镜像
# 构建上下文为仓库根目录（需打包跨组件的 controller/），在根目录执行：
#   docker build -t yunshenwuji/scriptcue-server:latest .
FROM python:3.13-slim

# 时区固化为东八区：审计日志与运行日志按本地时间呈现，便于演出排障。
# （同步机制基于 Unix 时间戳，本就不受时区影响；slim 镜像不含 tzdata，需显式安装）
ENV TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

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
# 注意：容器内包被扁平化复制为 /app/server（与源码三层布局不同），
# main.py 的"仓库根目录"推断会失效，因此主控端目录必须显式指定
ENV SC_DATA_DIR=/app/data
ENV SC_CONTROLLER_DIR=/app/controller

# 以非特权用户运行；固定 UID/GID 10001，便于绑定挂载宿主机目录时的属主管理
RUN groupadd -g 10001 scriptcue \
    && useradd -r -u 10001 -g scriptcue -s /usr/sbin/nologin scriptcue \
    && mkdir -p /app/data \
    && chown -R scriptcue:scriptcue /app/data
USER scriptcue

VOLUME /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
