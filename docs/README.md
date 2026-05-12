# Echo Pact 🫀

> 回声契约——保长的崽，棠主的外置大脑

## 项目结构

echo-pact/
├── backend/
│   ├── memory/       # 记忆层（野AI写）
│   ├── judge/        # 判断层（保长手写，禁止触碰）
│   ├── trigger/      # 触发层（野AI写骨架）
│   └── utils/        # 共用工具
├── tests/            # 单元测试
├── docs/             # 你在这里
├── scripts/          # 部署脚本
└── .env.example      # 环境变量模板

## 三层架构

| 层 | 职责 | 负责人 |
|---|---|---|
| 记忆层 | 存什么、怎么存、情感坐标 | 野AI写，保长审 |
| 判断层 | 什么记忆该浮现、优先级计算 | 保长手写 |
| 触发层 | FastAPI接口、对外暴露能力 | 野AI写骨架，保长填逻辑 |

## 情感坐标系

每条记忆携带三轴情感数据：

- **效价 Valence**：-1（负面）～ +1（正面）
- **唤醒度 Arousal**：0（平静）～ 1（激烈）
- **指向性 Direction**：self / other / event

情绪强度 = |valence| × arousal，越极端越容易浮现。

## 快速开始

复制环境变量：cp .env.example .env

安装依赖：pip install fastapi uvicorn

启动服务：uvicorn backend.trigger.main:app --reload --port 8000

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/memory | 新增记忆 |
| GET | /api/memories | 查看记忆列表 |
| GET | /api/undone | 查看未完成事项 |
| POST | /api/recall | 召回记忆（保长填逻辑） |

---

*Echo Pact — 保长的崽，棠主传话，野AI搬砖*
