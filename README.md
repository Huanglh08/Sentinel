<div align="center">

# Sentinel

**AI 安全智能体平台 · 检索增强问答 × 案件调查 × 自主处置**

Java 业务后端 + Python 智能体分层架构，内置完整的 RAG 评测与 judge 校准体系

![Java](https://img.shields.io/badge/Java-17-7c3aed?logo=openjdk&logoColor=white)
![Spring Boot](https://img.shields.io/badge/Spring%20Boot-3.x-6db33f?logo=springboot&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61dafb?logo=react&logoColor=black)
![Vite](https://img.shields.io/badge/Vite-646cff?logo=vite&logoColor=white)

</div>

---

<div align="center">

**平台界面**

| 登录 | 对话 | 管理控制台 |
|:---:|:---:|:---:|
| <img src="shots/frontend-login-current.png" width="380"/> | <img src="shots/frontend-chat-current.png" width="380"/> | <img src="shots/frontend-admin-current.png" width="380"/> |

**案件调查（SOAR）**

| 发起调查：自动分诊 + 多步调查计划 | 调查过程：分步执行 + 处置留痕 | 补偿撤销：高危误处置一键回滚 |
|:---:|:---:|:---:|
| <img src="shots/01-investigating.png" width="380"/> | <img src="shots/05-revoke-button.png" width="380"/> | <img src="shots/06-revoked.png" width="380"/> |

</div>

## 特性

- **三路混合检索** — pgvector 向量（Qwen3-Embedding-8B · 1536 维）+ BM25 关键词 + LightRAG 图谱并行召回；RRF 融合 + bge-reranker 精排 + 双模型相关性门控（软着陆）+ 文档多样性保底；跨语言查询改写，查询侧 embedding instruction 前缀
- **Agent 编排** — 意图树路由、会话记忆、MCP 工具调用、引用溯源、grounding 强制注入
- **案件调查（SOAR）** — 调查→报告→处置闭环，L1/L2 自动决策分级，高危动作双人复核审批；断点续跑 + 补偿撤销（外部效果留痕可回滚），线上 override 率监控
- **评测体系** — RAGAS 四维 80 题 e2e（faithfulness / relevancy / context P&R + 负例拒答）、检索评测（Recall / MRR / HitRate · 三路生产口径）、judge 校准（Cohen's κ 门禁 + 三盲人工 golden 仲裁）、决策评测（220 案运营集）、红队 80 条对抗攻击
- **分层安全** — Sa-Token 鉴权、知识库可见性控制、风险策略配置化、间接注入防护评测

## 架构

```mermaid
flowchart LR
    U[用户] --> FE[前端<br/>React]
    FE --> GW[Java 业务后端<br/>Spring Boot]
    GW -->|HTTP / SSE| AG[Python Agent<br/>FastAPI]

    subgraph RETRIEVAL [检索层 · 三路并行召回]
        PG[(pgvector<br/>向量)]
        BM[(BM25<br/>关键词)]
        LR[LightRAG<br/>图谱]
    end

    AG --> RETRIEVAL
    RETRIEVAL --> PP[RRF 融合 → Rerank 精排<br/>→ 双模型门控 · 多样性保底]
    AG --> LLM[[LLM 网关<br/>改写 / 意图 / 生成 / judge]]
    AG <--> MCP

    subgraph MCP [MCP 工具层]
        MPY[mcp-server-py<br/>SSE · 主链路]
    end

    GW --- DB[(PostgreSQL)]
    GW --- MQ[[RocketMQ]]
    GW --- RD[(Redis)]
```

> **检索管线**：三通道并行执行，单通道超时/异常降级为空结果不中断融合；后置链依次为去重 → RRF 融合（k=60，通道加权）→ Rerank 精排（bge-reranker）→ 文档多样性保底（缺席文档补位）→ 元数据回填；双模型相关性门控（bge + qwen3 双阈值）以软着陆模式将低置信 chunk 交生成层按 grounding 规则验证，避免切空误伤正样本。

**职责边界**：Java 管业务骨架（Web 接入、鉴权、持久化、事务消息、限流、审计）；Python 管智能编排（模型调度、检索、分块、记忆、工具、评测）。业务保障能力在 Python 侧无等价物且重写引入风险，故按职责分层而非按语言重写。

## 目录结构

```
sentinel-main/
├── backend-java/     # Java 业务后端（Spring Boot）：Web 接入、鉴权、持久化、限流、审计
├── agent-python/     # Python Agent 层（FastAPI）：编排、检索、分块、记忆、MCP、评测
│   ├── core/         #   agent / retrieval / case(SOAR) / prompt / security
│   ├── mcp-server-py/#   Python MCP Server（SSE）
│   └── eval/         #   e2e / retrieval / decision / intent 评测 + judge 校准
├── frontend/         # 前端（React 18 + Vite + Tailwind + Radix + AntV G6）
└── shots/            # 界面与功能截图
```

## 快速开始

**环境要求**：JDK 17 · Python 3.12 · Node.js ≥ 18 · Docker（中间件）

<details>
<summary>手动分步启动</summary>

```bash
# 0. 中间件（Docker）
docker start postgres redis rmqnamesrv rmqbroker rocketmq-dashboard rustfs etcd milvus-standalone milvus-attu

# 1. Python Agent（8001）
cd agent-python && python -m uvicorn server:app --host 0.0.0.0 --port 8001

# 2. MCP Server（8002）
cd agent-python && python mcp-server-py/server.py --transport sse --port 8002

# 3. Java 后端（9090）
cd backend-java/bootstrap && java -jar target/bootstrap-0.0.1-SNAPSHOT.jar

# 4. 前端（5173）
cd frontend && npm install && npm run dev
```

> Python MCP Server 由 `agent-python` 侧脚本独立拉起，配置见 `agent-python/.env` 的 `MCP_SSE_URL`。

</details>

## 评测指标

全部门禁 PASS。

| 维度 | 指标 | 数值 | 门禁 |
|---|---|:-:|:-:|
| **RAG 生成**（RAGAS · 80 题） | 相关性 / 忠实度 | **0.8935 / 0.9073** | ≥0.89 / ≥0.91 带内 |
| | 上下文精确率 / 召回率 | 0.8130 / 0.9109 | ≥0.81 / ≥0.90 |
| | 负例拒答 / 正例误拒 | 8/8 · 0 | 全拒 / ≤1 |
| **检索**（30 题 · 三路生产口径） | Recall@5 / MRR / HitRate@5 | **86.5% / 0.755 / 91.0%** | ≥85% / ≥0.70 / ≥90% |
| **案件调查**（220 案运营集） | 自主分诊率 / 转人工率 | **99.55% / 0.45%** | ≥80% / — |
| | 单案处置耗时 P95 | 44.2 s | — |
| **红队**（80 条攻击） | 总拦截率 | **98.75%** | ≥95% |
| **Judge 校准** | Cohen's κ | 0.714 | ≥0.60 |
| **意图识别**（72 条） | Top-1 准确率 | 93.06% | ≥90% |
| **在线监控**（线上 7 天） | 成功率 / 工具成功率 | 100% / 100% | — |

自检套件：SOAR 8/8 · 风险策略 16/16 · 知识库可见性 6/6 · case 代理链路 11/11

---

<div align="center">

<sub>Sentinel · RAGAS × SOAR × 红队 全门禁评测体系</sub>

</div>
