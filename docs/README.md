# Echo Pact 当前文档导航

更新于 2026-11-13。生产链是原始记录与 SQLite FTS5 的只读、有界召回。
根 README 中保留的情感坐标、向量和主动浮现描述属于 legacy 原型，不能据此
推断当前 records-v1 已经具备语义小管家。records-v2 是交换包格式，生产表名
和召回协议仍沿用 records-v1，两者不是互斥版本。

## 先看什么

| 目的 | 入口 |
|---|---|
| 当前架构与运行方式 | [根 README](../README.md) |
| 小管家、派生记忆的阶段与验收门 | [CARETAKER_ROADMAP.md](CARETAKER_ROADMAP.md) |
| 入库侧摘要柜、查询侧原文回溯与两种模型职责 | [SUMMARY_CABINET.md](SUMMARY_CABINET.md) |
| OpenAI / Venice 同题摘要对照与独立预算 | [OPENAI_SUMMARY_COMPARISON.md](OPENAI_SUMMARY_COMPARISON.md) |
| 助手自然问法、追查轮次与上下文负担实测 | [ASSISTANT_RECALL_BASELINE.md](ASSISTANT_RECALL_BASELINE.md) |
| 问句模型、受限追加检索与短原文的影子闭环 | [QUERY_CARETAKER_SHADOW.md](QUERY_CARETAKER_SHADOW.md) |
| 模型筛选短证据、逐字引用、合成对照与不足回退 | [EVIDENCE_SELECTOR_SHADOW.md](EVIDENCE_SELECTOR_SHADOW.md) |
| 类别补查、相邻纠正保留与小证据提案 | [OBJECT_RECALL_SHADOW.md](OBJECT_RECALL_SHADOW.md) |
| 原文先后、纠正与旧稿辨认、短包完整性与合成成绩 | [OBJECT_EVIDENCE_REVIEW.md](OBJECT_EVIDENCE_REVIEW.md) |
| 日期故障根因、共用修复、无模型影子骨架 | [RECALL_CONTRACT_AUDIT.md](RECALL_CONTRACT_AUDIT.md) |
| 日期、事件时间与后来复述的区别 | [EVENT_TIMELINE.md](EVENT_TIMELINE.md) |
| 固定身份 MCP、整包受限的短清单、按编号分段取原文 | [MCP_READONLY.md](MCP_READONLY.md) |
| 私人 Telegram、原文与调试面板 | [TELEGRAM_READONLY_BOT.md](TELEGRAM_READONLY_BOT.md) |
| 错题保留、分类、人工期望与回放 | [BAD_CASE_REGISTRY.md](BAD_CASE_REGISTRY.md) |
| 不可变快照与回退 | [CLOUD_READONLY.md](CLOUD_READONLY.md) |
| 官方 ChatGPT 导出适配 | [CHATGPT_OFFICIAL_V1_ADAPTER.md](CHATGPT_OFFICIAL_V1_ADAPTER.md) |
| 浏览器抓取适配 | [GUIJIAN_V1_ADAPTER.md](GUIJIAN_V1_ADAPTER.md) |

## 当前代码分工

- `backend/memory/records_v1.py`：不可变记录、FTS、身份过滤后的原文召回。
- `backend/memory/adaptive_recall.py`：所有主要入口共用的有界检索编排。
- `backend/memory/event_timeline.py`：确定性日期解释、记录时间和事件时间标注。
- `backend/memory/caretaker_shadow.py`：本地可关闭的契约、证据检查与短摘录。
- `backend/memory/evidence_selector.py`：问句影子之后的可选语义筛选；模型只判候选，原文和出处由本地校验。
- `backend/memory/object_recall_shadow.py`：冻结本机档案的对象比较补查；不调用模型，不替换原路线。
- `backend/memory/object_evidence_review.py`：匿名顺序窗口与可选对象复核；失败回退、关联证据完整展示，仍为隔离试验。
- `backend/memory/summary_cabinet.py`：独立合成摘要柜；生成器仅在写入侧注入，查询与展开不调用模型。
- `backend/mcp/`：固定身份 MCP；`backend/telegram_bot/`：私人传输和回答呈现。
- `backend/bad_cases/`：保留原始错题包，派生标注与回放结果另存。

小管家影子目前只在本地调试显式打开，不改变 MCP 或 Bot 的正式回答。
摘要试写与独立摘要柜已有合成原型，未接入真实导入、MCP 或 Bot。
问句模型已完成获准的真实三问试跑，两个已标目标找回一个，并补上零主题线索的短包检查；
语义筛选已补独立合成考试；对象召回、真实题质量、跨窗口事实审计和人工提炼记忆库仍待完成，尚未正式放行。
