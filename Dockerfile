# AgentForge 容器镜像。
# 参照 show 项目的做法：slim 基础镜像 + 阿里云源 + 依赖先装（利用层缓存）。
# 本项目依赖只有 requests/PyYAML/rich（全有预编译 wheel），pip 直装即可，
# 不需要 uv，也不需要 build-essential。
FROM python:3.11-slim

# 1. apt 换阿里云源（兼容 trixie 新格式和旧格式）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; \
                s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; \
                s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list; \
    fi

# 2. 只装 curl：compose 的 healthcheck 用它；不装 build-essential
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 3. 依赖先装：requirements.txt 不变时，下面的代码改动不触发重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    -r requirements.txt

# 4. 代码与静态资源（models.json/models.local.json 绝不进镜像——里面有 Key，
#    见 .dockerignore；云服务器上从 models.example.json 复制一份再填）
COPY agentforge/ ./agentforge/
COPY web/ ./web/
COPY skills/ ./skills/
COPY evals/tasks.yaml ./evals/tasks.yaml
COPY config.yaml models.example.json run.py webui.py ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8787

# 监听 0.0.0.0：容器里的 127.0.0.1 外面摸不到。
# 工作区用 -w 指到挂载卷（compose 里已给），不依赖镜像层的路径。
CMD ["python", "run.py", "--web", "--host", "0.0.0.0", "--port", "8787", "--no-open"]
