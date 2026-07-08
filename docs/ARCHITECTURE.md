# Architecture

## 1. 目标架构

```text
Browser
  -> Frontend Web App
  -> Backend API
  -> Knowledge Retrieval Service
       -> Full-text Search
       -> Vector Search
       -> Knowledge Graph Query
  -> LLM Provider
```

## 2. 前端

职责：

- 提供问答输入框
- 展示答案、引用、检索依据和反馈入口
- 管理会话状态
- 调用后端 API

## 3. 后端

职责：

- 接收用户问题
- 调用知识库检索接口
- 组织检索结果
- 调用大模型生成答案
- 返回结构化结果
- 记录日志和反馈

## 4. 知识库服务

职责：

- 全文搜索
- 向量检索
- 知识图谱实体/关系查询
- 返回统一格式的证据列表

## 5. 模型服务

初期通过 OpenAI-compatible API 调用模型。模型配置放在 `.env`：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

## 6. 部署草案

- 前端：静态资源或 Node 服务
- 后端：Python API 服务
- 知识库：独立服务
- 域名：指向云服务器入口
- HTTPS：通过 Nginx + 证书管理
