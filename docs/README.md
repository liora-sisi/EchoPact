# Echo Pact 当前文档导航

更新于 2026-11-28。生产链是原始记录与 SQLite FTS5 的只读、有界召回。
根 README 中保留的情感坐标、向量和主动浮现描述属于 legacy 原型，不能据此
推断当前 records-v1 已经具备语义小管家。records-v2 是交换包格式，生产表名
和召回协议仍沿用 records-v1，两者不是互斥版本。

## 先看什么

| 目的 | 入口 |
|---|---|
| 当前架构与运行方式 | [根 README](../README.md) |
| 小管家、派生记忆的阶段与验收门 | [CARETAKER_ROADMAP.md](CARETAKER_ROADMAP.md) |
| 最新顺序：先摘要与主助手直用闭环，再衡量额外查询小管家的收益 | [SUMMARY_FIRST_INTEGRATION_PLAN.md](SUMMARY_FIRST_INTEGRATION_PLAN.md) |
| 摘要门牌、事实和原话分层；生活/工程差异、模型同题验收 | [SUMMARY_CONTRACT.md](SUMMARY_CONTRACT.md) |
| DeepSeek / Venice / GPT 摘要候选、四类综合保真试卷与离线准备 | [SUMMARY_AUTHOR_COMPARISON.md](SUMMARY_AUTHOR_COMPARISON.md) |
| GPT / DeepSeek 较长对话对照、实际错误和整包尺寸 | [SUMMARY_AUTHOR_ROUND2.md](SUMMARY_AUTHOR_ROUND2.md) |
| Ombre 当前实现的事件整理、目录与原文边界对照 | [OMBRE_SUMMARY_REVIEW.md](OMBRE_SUMMARY_REVIEW.md) |
| 短卡与后台档案分开、精确展开、已有答卷零调用重放 | [SUMMARY_READING_CARDS.md](SUMMARY_READING_CARDS.md) |
| 状态/日期保真约定、六道合成实考、长对话遗漏与诊断尺寸 | [SUMMARY_FAITHFULNESS.md](SUMMARY_FAITHFULNESS.md) |
| 单段对话原/新规则对照、固定容量与尾部遗漏、后续分卡边界 | [SUMMARY_SINGLE_WINDOW_COMPARISON.md](SUMMARY_SINGLE_WINDOW_COMPARISON.md) |
| 按完整事件分卡、跨卡纠正联读、超限暂扣与逐字回查 | [SUMMARY_EVENT_BOOKLETS.md](SUMMARY_EVENT_BOOKLETS.md) |
| 事件作者首稿、日期原句及时间角色复验 | [SUMMARY_TIMELINE_REVIEW.md](SUMMARY_TIMELINE_REVIEW.md) |
| 保留首稿的有界校对、部分日期保真、正确对照与剩余阅读成本 | [SUMMARY_STATE_REVIEW.md](SUMMARY_STATE_REVIEW.md) |
| 按事件选读、必要纠正联读与完整阅读包的零调用体积对照 | [SUMMARY_EVENT_DELIVERY.md](SUMMARY_EVENT_DELIVERY.md) |
| 入库侧摘要柜、查询侧原文回溯与两种模型职责 | [SUMMARY_CABINET.md](SUMMARY_CABINET.md) |
| OpenAI / Venice 同题摘要对照与独立预算 | [OPENAI_SUMMARY_COMPARISON.md](OPENAI_SUMMARY_COMPARISON.md) |
| 助手自然问法、追查轮次与上下文负担实测 | [ASSISTANT_RECALL_BASELINE.md](ASSISTANT_RECALL_BASELINE.md) |
| 问句模型、受限追加检索与短原文的影子闭环 | [QUERY_CARETAKER_SHADOW.md](QUERY_CARETAKER_SHADOW.md) |
| 模型筛选短证据、逐字引用、合成对照与不足回退 | [EVIDENCE_SELECTOR_SHADOW.md](EVIDENCE_SELECTOR_SHADOW.md) |
| 类别补查、相邻纠正保留与小证据提案 | [OBJECT_RECALL_SHADOW.md](OBJECT_RECALL_SHADOW.md) |
| 原文先后、纠正与旧稿辨认、短包完整性与合成成绩 | [OBJECT_EVIDENCE_REVIEW.md](OBJECT_EVIDENCE_REVIEW.md) |
| 问句、检索、证据复核的整链小样与入口差别 | [CARETAKER_PIPELINE_SHADOW.md](CARETAKER_PIPELINE_SHADOW.md) |
| 归属、对象歧义、干扰与缺证四道新题的模型结果 | [CARETAKER_PIPELINE_CHALLENGE.md](CARETAKER_PIPELINE_CHALLENGE.md) |
| 真实旧题、当前短目录、逐字出处与新问法对照 | [CARETAKER_REAL_EVALUATION.md](CARETAKER_REAL_EVALUATION.md) |
| 对象不明与冲突同时存在时的表达、复考与剩余容量问题 | [CARETAKER_MULTIPLE_GAPS.md](CARETAKER_MULTIPLE_GAPS.md) |
| 重复出处共享、原文无损还原、完整组容量与旧答卷零调用重放 | [CARETAKER_COMPACT_PACKET.md](CARETAKER_COMPACT_PACKET.md) |
| 六道新问法、无书名号漏选、接收方解释越过原文的首次失败 | [CARETAKER_NOVEL_QUESTIONS.md](CARETAKER_NOVEL_QUESTIONS.md) |
| 下一阶段优先补原文片段语义索引、同预算混合检索与望舒会审意见 | [HYBRID_RECALL_SHADOW_PLAN.md](HYBRID_RECALL_SHADOW_PLAN.md) |
| 本地 BGE 片段索引、词面混合对照、真实旧题结果与未通过的门槛 | [SEMANTIC_RECALL_SHADOW.md](SEMANTIC_RECALL_SHADOW.md) |
| 命中窗口回取、等上限短卡、实际筛选四题与真实旧题本机装包 | [SEMANTIC_WINDOW_HANDOFF.md](SEMANTIC_WINDOW_HANDOFF.md) |
| 相邻更正、同消息多窗口、七份真实模型答卷与容量不足退回 | [SEMANTIC_ORDERED_HANDOFF.md](SEMANTIC_ORDERED_HANDOFF.md) |
| 先圈候选再补邻文、最多两次模型调用、五题首次结果与真实结构探针 | [SEMANTIC_SHORTLIST_HANDOFF.md](SEMANTIC_SHORTLIST_HANDOFF.md) |
| 规划/对象/向量/初选/复核整条隔离线路、统一预算与首次三题 2/3 | [SEMANTIC_CARETAKER_PIPELINE.md](SEMANTIC_CARETAKER_PIPELINE.md) |
| 中文模型复考、对象纠正初选修订、同材料复验及仍缺的证据 | [SEMANTIC_SHORTLIST_CORRECTION.md](SEMANTIC_SHORTLIST_CORRECTION.md) |
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
- `backend/memory/caretaker_pipeline.py`：同库同身份的整链影子入口；一次规划、至多一次证据复核，短包与完整审计分开。
- `backend/memory/caretaker_evaluation.py`：在本机分别计量目录可展开、已核原话可见及出处正确性，不把部分目标当成完整答案。
- `backend/memory/semantic_shadow.py`：独立本地片段向量、冻结原库指纹及 RRF 候选试验；不挂接正式入口。
- `backend/memory/semantic_evidence.py`：将原文偏移按身份/指纹回取为短卡，接现有筛选和引用短包；未接完整生产管线。
- `backend/memory/semantic_ordered_evidence.py`：从原库核对附近消息；整组装不下时保留原候选、延后复核，未接正式入口。
- `backend/memory/semantic_shortlist.py`：初选只圈编号，再为最多两条根记录补邻文并复核；两次有界回调，未接正式入口。
- `backend/memory/summary_cabinet.py`：独立合成摘要柜；生成器仅在写入侧注入，查询与展开不调用模型。
- `backend/mcp/`：固定身份 MCP；`backend/telegram_bot/`：私人传输和回答呈现。
- `backend/bad_cases/`：保留原始错题包，派生标注与回放结果另存。

小管家影子目前只在本地调试显式打开，不改变 MCP 或 Bot 的正式回答。
摘要试写与独立摘要柜已有合成原型，未接入真实导入、MCP 或 Bot。
问句模型已完成获准的真实三问试跑，两个已标目标找回一个，并补上零主题线索的短包检查；
语义筛选已补独立合成考试；对象召回、真实题质量、跨窗口事实审计和人工提炼记忆库仍待完成，尚未正式放行。
整链入口新增两道虚构题实际模型小样及一次旧真实题的零调用重放；前者两题预设检查通过，
后者保留已核纠正及对象歧义。M24 整链考试已开始，正式接入仍未放行。
2026-11-18 再跑四道预先冻结的虚构整链题，实际八次模型调用，预设检查 4/4；
有限样本与未解决的真实题分别报告，不把新题通过当作 M24 全面通过。
随后修复部分受限问法漏拦与“有什么不同”误拦，短包显式报告回退原因；
旧虚构题换问法回归完成两次模型调用并保留歧义，完整检查 915 项通过。
上述为查询管家阶段沿革；2026-11-25 已将摘要样板与主助手直用闭环提为当前优先级。
真实六问对照之后，固定三问已完成六次实际模型调用；筛选漏掉可见纠正的问题已补本地
相邻原文保护，零调用重放保住目标，完整检查 942 项通过。模型语义验收及正式接入仍未放行。
2026-11-20 [适用范围与对象纠正校准](CARETAKER_SCOPE_CALIBRATION.md)的四道虚构开发诊断题实际通过，
随后同卷真实三题的预设诊断检查 2/3 通过；换问法仍漏选关键纠正，保留失败与本地兜底记录。
最新完整回归 957 项通过；正式服务与摘要作者的验收门不变。
后续[同材料换问法诊断](CARETAKER_IDENTITY_PHRASING.md)补对象复核 v6：两道真实旧题均由模型选中纠正，
旧问法仍有缺口分类分歧；四道虚构对照通过，最新完整回归 966 项通过，仍为隔离试验。
2026-11-22 [对象复核 v8](CARETAKER_MULTIPLE_GAPS.md)可表达并存缺口与补证顺序：四道虚构题首次 3/4，
修订后同卷 4/4，两道真实旧题诊断 2/2；短包仍有容量暂扣，未宣称完整答全。全套回归 995 项通过。
随后[对象复核 v9 证据包减重](CARETAKER_COMPACT_PACKET.md)共享重复出处；旧答卷零调用重放后，
两道真实题六条所选证据与六条相邻语境均装入上限，同内容字节减少约四分之一。
最新全套回归 1,022 项通过；尚待未调试问法和实际读包验收，正式入口保持关闭。
随后[新自然问法检查](CARETAKER_NOVEL_QUESTIONS.md)补对象补查 v2 的无标题备用窗口。
六题实际整链严格 5/6；接收方机械检查 2/2、叙述复核 1/2，保留首次失败。
最新全套回归 1,043 项通过；缺口判分与名称到属性的推断边界仍待解决。

随后[本地片段向量首测](SEMANTIC_RECALL_SHADOW.md)跑通真实中文 embedding：合成目标 Top-3
命中 1/6→6/6，有限真实旧题样本 Top-10 命中 1/4→2/4。首次结果保留；加入出处冻结、
输出目录保护与查询中撤权检查后，同题结果不变。全套回归 1,067 项通过、1 项跳过。
尚未接小管家筛选、最终短包或正式服务，未在 VPS 安装模型。

下一节[窗口短包接头](SEMANTIC_WINDOW_HANDOFF.md)已接上现有筛选模型：四道专用虚构题
两臂实际八次调用，向量窗口臂 4/4；输出 752–1,237 字节。严格字段验证后的原答卷
零调用复放逐项不变，完整回归 1,086 项通过。真实旧题只在本机装包，仍保留复杂对象缺口；
完整 planner/对象补查/相邻语境管线与正式入口尚未接入。

[相邻更正组件](SEMANTIC_ORDERED_HANDOFF.md)随后补上原始分支顺序与跨消息条件保留。
七道虚构组件题首轮 6/7；本地契约修正后同答卷零调用复放 7/7，前六份不变。
真实旧题发现扩邻文挤掉有效种子卡，已用完整退回保护候选；六个真实包仍延后语境审计。
完整检查 1,105 项通过。下一步先缩小候选再补邻文，不把这一组件成绩当作正式放行。

[两步初选组件](SEMANTIC_SHORTLIST_HANDOFF.md)已接起：五道固定虚构题首次实际 5/5，
十次调用；第八条候选及其更正能完整递出，待查线索不会被冒充已查完。
真实手账只做已知目标的本机接头验证，完整回归 1,122 项通过；完整管线和真实自动初选待验。

[完整隔离线路](SEMANTIC_CARETAKER_PIPELINE.md)随后接通规划、词面/对象/向量与两步复核。
[真实旧题验收](SEMANTIC_REAL_ACCEPTANCE.md)两道有效旧题中一道部分目标可见，对象初选仍有遗漏。
2026-11-25 [输入勘误](SEMANTIC_INPUT_INTEGRITY.md)查明新问法及两个对照丢失了汉字；
完整中文目标复验为纯向量第 1、合流第 2，不能沿用旧的第 286 名判断引擎。
输入修复后完整回归 1,162 项通过；[独立留出卷协议](CARETAKER_HOLDOUT_PROTOCOL.md)已列清评分和防串题规则，
真盲卷、全库索引、接收方理解与正式接入继续待验。

后续[初选纠正复验](SEMANTIC_SHORTLIST_CORRECTION.md)完成有效中文手账整链及对象旧卷复考：
手账原句可见，对象纠正不再被延后，仍保留对象身份和答案缺口。虚构初选三题首次 3/3，
UTF-8 执行条件下完整检查 1,162 项通过、1 项跳过；真实题仅是已知部分目标开发检查。
