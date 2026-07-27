# 宏映AI Python智能视频平台 V1.0

本仓库实现《宏映AI Python智能视频平台详细设计 V1.0》定义的内部智能媒体
平台：素材探测与分析、结构化创意规划、Timeline V1 校验与编译、FFmpeg
确定性渲染、技术质量检测、运行查询与取消，以及 RabbitMQ/MinIO/Redis/MySQL
生产适配器。

## 1.0 服务边界

代码按职责分为四个稳定入口：

- `hongying_ai.parser`（ai-parser）：图片、视频、音频解析、封面、200/400
  缩略图、480P 代理、镜头切分和基础标签/质量特征。
- `hongying_ai.composer`（ai-composer）：Timeline 编译、图片转视频、裁剪、
  背景模糊、转场、字幕、Logo、BGM、原声混音、FFmpeg 合成和技术质检。
- `hongying_ai.intelligence`（ai-intelligence）：DeepSeek 创意规划、规则降级、
  模板、素材评分匹配、品牌知识检索和合规辅助。
- `hongying_ai.common`（ai-common）：配置、MinIO、RabbitMQ、Redis、MySQL、
  FFmpeg、安全路径和公共契约。

Java 仍负责正式业务编排和业务数据持久化。FastAPI Studio 是运营/联调入口，
只提交计算任务和展示计算结果，不替代商户、活动、订单等 Java 业务服务。

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

打开 [http://localhost:8080/studio](http://localhost:8080/studio) 即可使用一键
成片工作台。完整流程为：

```text
选择商户/活动/模板/素材
  → Planner 生成 Brief、Storyboard、基础 Timeline
  → 模板应用和 Timeline Schema 校验
  → 标签、质量、授权和时长规则匹配
  → Composer 执行 FFmpeg
  → Quality 执行技术质量检测
  → 视频、封面、预览、质量报告和 Manifest 写入 MinIO
```

MinIO 管理控制台为 `http://localhost:9003`，S3 API 为
`http://localhost:9000`，默认桶为 `hongying`。MinIO SDK 必须连接 S3 API，
不能连接 9003 控制台端口。

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
| GET | `/internal/v1/studio/templates` | 查询一键成片模板 |
| GET | `/internal/v1/studio/assets` | 查询租户素材目录 |
| POST | `/internal/v1/studio/assets/upload` | 上传并解析图片/视频/音频 |
| POST | `/internal/v1/studio/generations` | 启动一键成片工作流 |
| GET | `/internal/v1/studio/objects` | 获取租户隔离的作品临时地址 |

运行后可在 `/internal/docs` 查看 OpenAPI。事件通道见
`schemas/asyncapi.yaml`；执行 `tools/export_schemas.py` 可生成所有 Pydantic
契约的 JSON Schema。

## 能力边界

V1.0 一键成片所需的图片/视频解析、模板、素材规则匹配、字幕、Logo、BGM、
转场、合成和质量门禁已经形成可执行闭环。基础标签和主体框使用可解释的规则
与图像算法输出，并带 `source/modelStatus` 标识。OCR、ASR、语义视觉模型、
TTS 和二维码视觉审核仍属于可插拔增强能力，不会伪装成真实模型结果。
