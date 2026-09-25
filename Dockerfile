# syntax=docker/dockerfile:1
# bingops 巡检日报 Agent 运行镜像：report（K8S CronJob）与 bot（Deployment + Service）共用。
# 构建：docker build -t inspection-agent:latest .
# 本地验证：docker run --rm inspection-agent:latest report --config /app/config/inspection.yaml --dry-run
# Helm 用法：CronJob args=["report", ...]；bot Deployment args=["bot"] + Service 暴露 bot.port
#            （平台需配置 BINGOPS_AGENT_CALLBACK_URL 指向该 Service 的 /feishu/events）

FROM python:3.13-slim AS builder

WORKDIR /build

# 先拷贝清单与源码；依赖未变时命中层缓存
COPY pyproject.toml ./
COPY src ./src

# 安装到独立 prefix，运行阶段整目录搬运（site-packages 路径结构一致）
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="inspection-agent" \
      org.opencontainers.image.description="bingops 巡检日报 Agent（六场景之场景 3）编排层" \
      org.opencontainers.image.source="https://git.example.com/group/inspection-agent"

# TZ 仅影响 localtime 显示；业务统计窗口时区由 config.schedule.timezone（ZoneInfo）决定
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /install /usr/local

WORKDIR /app

# 运行时技能目录（bot 技能：方法论 prompt 段 + 工具子集声明，bot/skillregistry.py 加载）
COPY skills ./skills

# 非 root 运行；config/（ConfigMap 挂载点，样例见仓库 config/inspection.example.yaml，
# 镜像内不内置配置）与 out/（产物目录）需可写
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/config /app/out \
    && chown -R appuser:appuser /app
USER appuser

# bot 形态监听端口（POST /feishu/events + GET /healthz）；report 形态不监听
EXPOSE 8080

# 凭据一律由 Secret 注入环境变量（SKILL 红线）：
#   pipeline：BINGOPS_AGENT_TOKEN（或 USERNAME/PASSWORD）、GITLAB_TOKEN
#   出站/对话：FEISHU_REPORT_CHAT_ID、LLM_API_KEY
#   OSS：OSS_ENDPOINT / OSS_BUCKET / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET
# 入口二选一（Helm args 覆盖 CMD）：
#   CronJob    args: ["report", "--config", "/app/config/inspection.yaml"]
#   Deployment args: ["bot",    "--config", "/app/config/inspection.yaml"]
ENTRYPOINT ["python", "-m", "inspection_agent"]
CMD ["bot", "--config", "/app/config/inspection.yaml"]
