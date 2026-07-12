# geowiki

一款专注地质领域的百科全搜。

## 当前阶段

当前版本为 **v2.0.0**。已具备私有知识库问答、受控 Agentic RAG、基本/深度双模式、持久化跨文档研究任务、邀请码与邮箱验证注册、登录会话、用户 API Key、每日次数配额、会话历史、标准目录、开发者控制台和管理员基础入口。

基本模式检索链路为：领域门控 -> 机械归一/定义槽位 -> 复杂问题 DeepSeek 规划 -> Schema/FTS/KG/ANN 混合检索 -> 证据审查 -> 最多一次补充检索 -> 受证据约束的回答。深度模式使用独立异步流程：研究规划 -> Schema/目录候选枚举 -> 逐文件限定检索 -> AND 证据组校验 -> 分批结构化事实抽取 -> 对比矩阵与覆盖说明。

## 文档

- `docs/PRD.md` - 产品需求文档
- `docs/ARCHITECTURE.md` - 技术架构草案
- `docs/API_SPEC.md` - 前后端与知识库接口约定
- `docs/OPENAPI_QUICKSTART.md` - 公开 QA API 调用说明和示例
- `docs/WIREFRAMES.md` - 页面原型说明
- `docs/KNOWLEDGE_BASE_REQUIREMENTS.md` - 知识库构建要求
- `docs/LICENSING_AND_REPOSITORIES.md` - 双许可证与仓库策略
- `docs/V1_RELEASE.md` - v1.0 架构、验收结果与私有数据边界
- `docs/V2_RELEASE.md` - v2.0.0 双模式、定义问答、深度研究与配额升级说明
- `docs/M10_RETRIEVAL_EVALUATION.md` - v1.0.6 查询改写、Multi-Query、MMR 与 ANN 评估结果

## 本地配置

`.env` 仅用于本地模型/API 配置，不提交到 Git。

示例：

```text
OPENAI_API_KEY=<your-model-api-key>
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
DEFINITION_ANSWER_MAX_TOKENS=1600
RESEARCH_PLANNER_MAX_TOKENS=1000
RESEARCH_ANALYSIS_MAX_TOKENS=1800
RESEARCH_ANALYSIS_BATCH_SIZE=4
RESEARCH_ANSWER_MAX_TOKENS=2200
RESEARCH_MAX_DOCUMENTS=60
RESEARCH_DOCUMENT_CONCURRENCY=4
RESEARCH_GLOBAL_CONCURRENCY=1
KNOWLEDGE_BASE_URL=
ENABLE_SYNC_WEB_SUPPLEMENT=false
API_KEYS=
API_KEY_REGISTRY_PATH=
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=30
APP_DB_PATH=data/app/application.sqlite
AUTH_REQUIRED=true
REGISTRATION_ENABLED=true
SESSION_COOKIE_NAME=kb_session
SESSION_COOKIE_SECURE=false
SESSION_TTL_HOURS=168
DAILY_QUOTA_DEFAULT=10
QUOTA_TIMEZONE=Asia/Shanghai
EMAIL_VERIFICATION_ENABLED=true
EMAIL_VERIFICATION_SECRET=<long-random-secret>
EMAIL_CODE_TTL_MINUTES=10
EMAIL_CODE_COOLDOWN_SECONDS=60
EMAIL_CODE_DAILY_LIMIT=5
EMAIL_DEBUG=false
EMAIL_PROVIDER=agentmail
AGENTMAIL_API_KEY=am_your_token
AGENTMAIL_INBOX_ID=geowiki@agentmail.to
AGENTMAIL_BASE_URL=https://api.agentmail.to/v0
```

`KNOWLEDGE_BASE_URL` 为空时，系统不会编造答案，会返回证据不足提示。知识库服务完成后，填入知识库后端地址即可接入 `/knowledge/search` 和 `/knowledge/standards`。

稠密向量使用阿里云百炼 `text-embedding-v4`，运行时通过 USEARCH ANN 索引检索，不再逐条解析 SQLite 中的 JSON 向量。v1.0.6 默认使用 `ANN_EXPANSION_SEARCH=64`；embedding 请求按 `EMBEDDING_BATCH_SIZE` 分批并复用连接。完成或更新 `chunk_embeddings` 后重建私有索引：

```bash
PYTHONPATH=src .venv/bin/python scripts/build_ann_index.py
```

索引默认写入 `data/knowledge_base/indexes/`，与 SQLite 知识库一样属于私有资产，不提交 Git。

本地知识库证据不足时，默认快速返回证据不足，并记录知识库缺口任务；后台后续再进行官方来源补充、OCR 和候选入库审核。若设置 `ENABLE_SYNC_WEB_SUPPLEMENT=true`，同步请求会尝试查询国家标准公开系统和自然资源标准化信息服务平台，但仍不会在缺少正文证据时生成条款级结论。

联网或 OCR 新获得的数据应先进入候选暂存区，管理员确认后才进入正式知识库和索引。

## 本地运行

安装依赖：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

将 AgentMail token 写入 `.env` 后，创建或复用 `geowiki` 发件箱并生成验证码签名密钥：

```bash
PYTHONPATH=src .venv/bin/python scripts/setup_agentmail.py
```

token、验证码签名密钥和 `.env` 都不得提交到 Git。

启动 API：

```bash
PYTHONPATH=src uvicorn mining_qa.api:app --host 127.0.0.1 --port 8000
```

打开页面：

```text
http://127.0.0.1:8000
```

首次使用先创建管理员账号和邀请码：

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py create-admin --account admin --display-name 管理员
PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py create-invite --label "第一轮内测" --admin-account admin
```

管理员创建时会安全提示输入密码；邀请码明文只显示一次。注册用户默认每天拥有 10 个配额单位，网页问答和用户 API Key 共用。基本模式消费 1 个单位；深度模式消费 3 个单位；从同一基本答案一键升级只追加 2 个单位。领域外拒答不消费，系统异常和排队阶段取消的深度任务退回预留单位。

管理员可以修改长期日上限，或给指定用户增加当天次数：

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py set-daily-limit --account user@example.com --limit 20 --reason "扩大测试范围" --admin-account admin
PYTHONPATH=src .venv/bin/python scripts/manage_accounts.py add-quota --account user@example.com --count 5 --reason "专项测试" --admin-account admin
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

命令行提问：

```bash
PYTHONPATH=src python -m mining_qa "哪个标准规定了金矿基本工程间距？"
```

API 调用示例：

```bash
curl -X POST http://127.0.0.1:8000/api/ask \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: kb_live_xxx' \
  -d '{"question":"哪个规范规定了铁矿的推荐工程间距？"}'
```

创建深度研究任务：

```bash
curl -X POST http://127.0.0.1:8000/api/research/tasks \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: kb_live_xxx' \
  -d '{"question":"不同矿种规范对矿体无限外推所依据的间距有哪些代表性差异？"}'
```

使用返回的 `task_id` 查询 `/api/research/tasks/{task_id}`；任务完成后读取 `/api/research/tasks/{task_id}/result`。

默认要求登录或用户 API Key。用户登录网页后在“开发者”页面创建密钥，网页问答和该账号的全部 API Key 共用每日次数。调用日志写入本地 `data/api_calls.jsonl`，不会提交到 Git。

旧 API Key registry 仅用于内部回归和兼容，默认写入 `data/api_keys.json`，只保存 key hash 和元数据：

```bash
PYTHONPATH=src .venv/bin/python scripts/manage_api_keys.py create --name test-client --purpose "local testing"
PYTHONPATH=src .venv/bin/python scripts/manage_api_keys.py list
PYTHONPATH=src .venv/bin/python scripts/manage_api_keys.py disable key_xxxxxx
PYTHONPATH=src .venv/bin/python scripts/manage_api_keys.py enable key_xxxxxx
```

创建命令输出的 `api_key` 只显示一次，需要当场保存。`.env` 中的 `API_KEYS` 仍作为 legacy key 兼容。

交互式 OpenAPI 文档：

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/redoc
http://127.0.0.1:8000/openapi.json
```

更多调用示例见 `docs/OPENAPI_QUICKSTART.md` 和 `examples/api_client.py`。

限流默认启用，优先使用 Redis；Redis 不可用时自动降级到本地内存限流。本地安装 Redis：

```bash
sudo apt-get update
sudo apt-get install -y redis-server
sudo systemctl enable --now redis-server
redis-cli ping
```

用量统计：

```bash
curl http://127.0.0.1:8000/api/usage -H 'X-API-Key: kb_live_xxx'
```

## 知识库 Mock 与回归测试

在真实知识库接入前，可以用内置 mock 服务验证 API 对接：

```bash
PYTHONPATH=src uvicorn mining_qa.mock_kb:app --host 127.0.0.1 --port 18081
```

另一个终端启动主 API：

```bash
KNOWLEDGE_BASE_URL=http://127.0.0.1:18081 \
API_KEYS=dev-local-key \
PYTHONPATH=src uvicorn mining_qa.api:app --host 127.0.0.1 --port 8000
```

自动回归测试：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python scripts/evaluate_ann_recall.py
KB_URL=http://127.0.0.1:18181 API_URL=http://127.0.0.1:18180 \
  PYTHONPATH=src .venv/bin/python scripts/run_kb_regression.py
KB_URL=http://127.0.0.1:18181 API_URL=http://127.0.0.1:18180 \
  PYTHONPATH=src .venv/bin/python scripts/run_api_regression.py
```

## 许可证与数据边界

计划采用双许可证路线：

- 社区版：AGPL-3.0
- 企业版：Commercial License

公开社区版只分发工具、schema、OCR/入库流程、接口和示例数据，不分发真实标准全文、OCR 后全文、预构建标准知识库或向量库。
