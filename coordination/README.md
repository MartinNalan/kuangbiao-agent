# Agent Coordination

本目录用于 PRD/产品 agent 与知识库构建 agent 之间的轻量协作。

## 角色

- PRD agent: 维护 PRD、架构、API、知识库需求文档，回答产品规则和接口约束问题。
- KB agent: 构建知识库，负责 PDF 识别、schema、入库、检索、OCR、标准目录查询和样例结果。
- User: 在两个 agent 之间提醒对方读取本目录，不需要人工复述技术细节。

## 工作规则

1. 每次开始工作前，先读取本目录所有文件。
2. 有进展就更新 `kb_agent_status.md` 或 `prd_agent_answers.md`。
3. 有问题就写入 `kb_agent_questions.md`，不要只写在聊天窗口。
4. 已确认的重要决定写入 `decisions.md`。
5. 任务状态统一写入 `task_board.md`。
6. 不要把 `.env`、API Key、token 或私密文件写进本目录。
7. 不要覆盖对方刚写的内容；追加新段落，并注明日期、角色和状态。

## Onboarding

给 KB/OCR agent 的完整提示词见：

```text
coordination/kb_agent_onboarding_prompt.md
```

当前约束：如果 KB/OCR agent 仍在执行用户直接下达的 OCR 任务，应先完成 OCR。读取协作文档后只需理解需求和记录状态，不要立即启动知识库搭建，除非用户明确要求。

## 状态格式

建议使用：

```text
Date: 2026-07-08
Role: KB agent
Status: in_progress
Summary:
- ...
Next:
- ...
Blocked:
- ...
```

## 问题格式

建议使用：

```text
Question ID: KB-Q001
From: KB agent
To: PRD agent
Status: open
Question:
...
Context:
...
Needed by:
...
```

回答后把状态改为 `answered`，或在 `prd_agent_answers.md` 中引用 Question ID。
