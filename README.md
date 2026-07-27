# 宏映AI Python智能视频平台 V1.0

本仓库实现《宏映AI Python智能视频平台详细设计 V1.0》定义的内部智能媒体
平台：素材探测与分析、结构化创意规划、Timeline V1 校验与编译、FFmpeg
确定性渲染、技术质量检测、运行查询与取消，以及 RabbitMQ/MinIO/Redis/MySQL
生产适配器。

## 已实现的部署单元

- `ai-api`：FastAPI 内部 API、健康检查、指标、Timeline 校验、渲染预检、
  Run 查询和取消。
- `ai-parser-worker`：素材租户前缀/大小/摘要校验、FFprobe 媒体画像和分层
  分析 Manifest。
- `ai-planner-worker`：DeepSeek 结构化规划、Prompt 注入隔离、预算限制、
  规则模板降级、素材确定性匹配和 Timeline 生成。
- `ai-composer-worker`：租约、幂等、心跳、素材校验、受控 filter graph、
  多片段/转场/叠加/字幕/音频、进度事件、技术 QC、临时 Key 原子发布。
- `ai-quality-worker`：独立技术、黑帧、冻结和静音检测，生成版本化
  `QualityReport`。
- 基础设施：RabbitMQ topic/DLQ、Redis 租约/取消/幂等、MinIO 对象存储、
  MySQL Run/模型调用/成本/Prompt 表，以及 DeepSeek 模型网关。

所有 FFmpeg 调用均使用参数数组和生成的 filter 脚本，不使用
`shell=True`。Object Key 必须位于 `prod/{tenantId}/` 或
`tenant/{tenantId}/` 前缀下，且素材必须出现在 `InputManifest` 中。

## 本地启动

要求 Python 3.12+、FFmpeg/FFprobe。不要把真实密钥提交到 Git。

```bash
cp .env.example .env
# 编辑 .env，填写本地基础设施和模型凭证
uv sync --extra dev
uv run python tools/export_schemas.py
uv run pytest
```

使用现有 MySQL/Redis/RabbitMQ/MinIO 时：

```bash
mysql -h 127.0.0.1 -u root -p ai_platform < migrations/001_initial.sql
uv run uvicorn hongying_ai.api:app --host 0.0.0.0 --port 8080
WORKER_KIND=parser uv run hongying-worker
WORKER_KIND=planner uv run hongying-worker
WORKER_KIND=composer uv run hongying-worker
WORKER_KIND=quality uv run hongying-worker
```

也可由 `docker compose up --build` 启动完整依赖和五个部署单元。生产环境应将
凭证改为密钥管理挂载，固定镜像摘要，并为 Composer 配置独立 CPU/GPU 池和
工作卷配额。

## 内部 REST API

除健康检查和指标外，接口必须携带：

```text
X-Service-Name: video-task-service
X-Tenant-Id: 10001
X-Trace-Id: trace_...
```

| 方法 | 路径 | 超时语义 |
| --- | --- | --- |
| POST | `/internal/v1/media/probe` | 轻量探测，3 秒 |
| POST | `/internal/v1/timelines/validate` | 纯校验，2 秒 |
| POST | `/internal/v1/renders/preflight` | 资源估算，3 秒 |
| GET | `/internal/v1/runs/{runId}` | 只读查询 |
| POST | `/internal/v1/runs/{runId}:cancel` | 幂等取消标记 |
| GET | `/internal/health/liveness` | 进程存活 |
| GET | `/internal/health/readiness` | MySQL/Redis/MQ/MinIO |
| GET | `/internal/metrics` | Prometheus 指标 |

运行后可在 `/internal/docs` 查看 OpenAPI。事件通道见
`schemas/asyncapi.yaml`；执行 `tools/export_schemas.py` 可生成所有 Pydantic
契约的 JSON Schema。

## 能力边界

V1.0 的媒体执行、契约、状态可靠性、模型规划和技术质量链路均可运行。OCR、
ASR、视觉检测、向量检索、TTS 和内容安全模型通过分析 Manifest/Agent
接口预留了版本化输出位；要获得真实推理结果，仍需接入拥有合法授权的具体
模型供应商或私有模型，不能用空凭证伪造生产结果。平台会保留空结果和模型
版本记录，不会让未配置能力绕过质量或合规门禁。

