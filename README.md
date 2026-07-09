# Mining Knowledge QA

矿产资源专业知识库问答产品规划与原型项目。

## 当前阶段

本项目先用于沉淀产品需求、接口约定、架构方案和原型说明。知识库检索服务可在独立项目中实现，后续通过 API 接入。

## 文档

- `docs/PRD.md` - 产品需求文档
- `docs/ARCHITECTURE.md` - 技术架构草案
- `docs/API_SPEC.md` - 前后端与知识库接口约定
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
```

`KNOWLEDGE_BASE_URL` 为空时，系统不会编造答案，会返回证据不足提示。知识库服务完成后，填入知识库后端地址即可接入 `/knowledge/search` 和 `/knowledge/standards`。

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

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

命令行提问：

```bash
PYTHONPATH=src python -m mining_qa "哪个标准规定了金矿基本工程间距？"
```

## 许可证与数据边界

计划采用双许可证路线：

- 社区版：AGPL-3.0
- 企业版：Commercial License

公开社区版只分发工具、schema、OCR/入库流程、接口和示例数据，不分发真实标准全文、OCR 后全文、预构建标准知识库或向量库。
