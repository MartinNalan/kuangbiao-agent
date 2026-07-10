# Mining Knowledge QA

矿产资源专业知识库问答产品规划与原型项目。

## 当前阶段

本项目先用于沉淀产品需求、接口约定、架构方案和原型说明。知识库检索服务可在独立项目中实现，后续通过 API 接入。

## 文档

- `docs/PRD.md` - 产品需求文档
- `docs/ARCHITECTURE.md` - 技术架构草案
- `docs/API_SPEC.md` - 前后端与知识库接口约定
- `docs/OPENAPI_QUICKSTART.md` - 公开 QA API 调用说明和示例
- `docs/WIREFRAMES.md` - 页面原型说明
- `docs/KNOWLEDGE_BASE_REQUIREMENTS.md` - 知识库构建要求
- `docs/LICENSING_AND_REPOSITORIES.md` - 双许可证与仓库策略

## 本地配置

`.env` 仅用于本地模型/API 配置，不提交到 Git。

示例：

```text
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash
KNOWLEDGE_BASE_URL=
ENABLE_SYNC_WEB_SUPPLEMENT=false
API_KEYS=dev-local-key
API_KEY_REGISTRY_PATH=
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_ENABLED=true
RATE_LIMIT_PER_MINUTE=30
```

`KNOWLEDGE_BASE_URL` 为空时，系统不会编造答案，会返回证据不足提示。知识库服务完成后，填入知识库后端地址即可接入 `/knowledge/search` 和 `/knowledge/standards`。

本地知识库证据不足时，默认快速返回证据不足，并记录知识库缺口任务；后台后续再进行官方来源补充、OCR 和候选入库审核。若设置 `ENABLE_SYNC_WEB_SUPPLEMENT=true`，同步请求会尝试查询国家标准公开系统和自然资源标准化信息服务平台，但仍不会在缺少正文证据时生成条款级结论。

联网或 OCR 新获得的数据应先进入候选暂存区，管理员确认后才进入正式知识库和索引。

## 本地运行

安装依赖：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

启动 API：

```bash
PYTHONPATH=src uvicorn mining_qa.api:app --host 127.0.0.1 --port 8000
```

打开页面：

```text
http://127.0.0.1:8000
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
  -H 'X-API-Key: dev-local-key' \
  -d '{"question":"哪个规范规定了铁矿的推荐工程间距？"}'
```

`API_KEYS` 为空且没有 API Key registry 时用于本地开发，不启用 API Key 鉴权；配置后，`/api/*` 需要通过 `X-API-Key` 或 `Authorization: Bearer <key>` 调用。调用日志写入本地 `data/api_calls.jsonl`，不会提交到 Git。

API Key registry 支持本地可管理 key，默认写入 `data/api_keys.json`，只保存 key hash 和元数据，不保存明文 key：

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
curl http://127.0.0.1:8000/api/usage -H 'X-API-Key: dev-local-key'
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
.venv/bin/python scripts/run_api_regression.py
```

## 许可证与数据边界

计划采用双许可证路线：

- 社区版：AGPL-3.0
- 企业版：Commercial License

公开社区版只分发工具、schema、OCR/入库流程、接口和示例数据，不分发真实标准全文、OCR 后全文、预构建标准知识库或向量库。
