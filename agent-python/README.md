# agent-python — Agent 智能层 (FastAPI)

Ragent 架构中**迁至 Python** 的部分，作为独立 FastAPI 服务运行，由 `backend-java` 通过 HTTP/MCP 调用。

## 目录结构
```
agent-python/
├── server.py            # FastAPI 入口：/chat(SSE) /ingest /search /health
├── core/
│   ├── llm.py           # LLM 客户端（httpx 异步，OpenAI 兼容；chat/stream）
│   ├── intent.py        # 意图树 + LLM 打分识别（KB/MCP/SYSTEM），降级兜底
│   ├── rewrite.py       # 查询改写 + 子问题拆分，LLM 失败降级原问题
│   ├── mcp_client.py    # MCP 工具调用客户端（sse/stdio），失败降级
│   ├── agent.py         # 编排主链路：rewrite→intent→dispatch→generate
│   ├── parser/          # 文档解析（md/csv/txt 标准库；pdf/docx/xlsx 懒加载）
│   ├── chunk/           # 分块（整文档 / block-aware 语义分块）
│   └── retrieval/       # ✅ 多通道检索引擎（向量+关键词并行 / RRF 融合 / 可插拔 Rerank / 预算三段式）
├── mcp-server-py/       # ✅ 已完成：FastMCP 实现的 MCP 工具（weather/ticket/sales/youcom）
└── eval/                # （待接入）RAG 评估（ragas/deepeval）
```

## 多通道检索引擎（core/retrieval，对应 rag-core RetrievalServiceImpl）
```
           query
             │
   ┌─────────┼──────────┐
   ▼         ▼          ▼
Vector     Keyword     [Web/Graph]    ← 通道并行（asyncio.gather + 超时）
   │         │          │
   └────┬────┴────┬─────┘
        ▼         ▼
     RRF 融合   去重(同 doc_id)
        │
        ▼
   candidateLimit 截断
        │
        ▼
   可插拔 Rerank（LLM/交叉编码器/None）
        │
        ▼
   contextTopK 截断
        │
        ▼
   RetrievedChunk[]  →  LLM context
```

- **RRF 融合**：`score = Σ_c weight_c / (k + rank_c)`；按通道原始名次累加，权重区分通道可信度
- **预算三段式**：`recallBudget`（每通道扇出）→ `candidateLimit`（送 Rerank 上限）→ `contextTopK`（进 LLM 条数）
- **通道降级**：单通道超时/异常 → 返回空，不影响其他通道
- **单通道不融合**：避免 RRF 退化为空操作

**默认生产栈**（`core/retrieval/retriever.py`，无阉割）：

| 通道 | 实现 | 对应 Java 生态 |
|---|---|---|
| 向量嵌入 | `RoutingEmbeddingService`（默认 `Qwen/Qwen3-Embedding-8B` @ 硅基流动，1536 维；候选链 qwen-emb-8b → qwen-emb-local（Ollama）→ text-embedding-3-large（AIHubMix）；含 2/30s 熔断） | ragent infra-ai.embedding 家族 |
| 向量索引 | `FaissVectorBackend`（faiss-cpu `IndexFlatIP`，归一化后内积 = 余弦） | Milvus / ES dense |
| 关键词 | `BM25Backend`（rank-bm25 `BM25Okapi` + jieba 分词，缺失 jieba 自动退化为字符 bigram） | Lucene / ES BM25 |
| 精排 | `CrossEncoderReranker`（`BAAI/bge-reranker-base`） | 交叉编码器精排 |

**可插拔后端**（生产可替换）：
- `VectorBackend` / `KeywordBackend` / `Reranker` 均为抽象接口
- 接入 Milvus：实现 `MilvusVectorBackend(VectorBackend)`，构造时换掉 `HybridRetriever.vector`
- 接入 ES：实现 `EsKeywordBackend(KeywordBackend)`，构造时换掉 `HybridRetriever.keyword`
- 跳过 Rerank：构造 `Reranker` 时传 `None` / `NoOpReranker`

**依赖行为**：
- 三个生产依赖（`sentence-transformers` / `faiss-cpu` / `rank-bm25`）任一缺失 → 抛 `ImportError` 并打印 `pip install ...` 命令
- **不静默退化到伪 embedding / 伪 BM25**——保证不出现"测试通过但生产语义错乱"

**调用入口**：
- `core/agent.py` KB 检索：从意图节点的 `collection_names` 取出定向作用域，调用 `engine.retrieve(question, cols, budget)`
- `server.py` `POST /search`：纯检索端点，参数 `query/top_k/collections`

## Agent 主链路（对应 ragent-main rag/core）
`core/agent.py` 编排：
```
用户问题
  → QueryRewriter.rewrite()   问题改写 + 拆子问题（失败降级原问题）
  → IntentResolver.classify() 每个子问题意图打分（失败降级空意图）
  → 分派：MCP 意图 → MCPToolClient 调工具；KB 意图 → retriever 检索
  → LLM 流式生成（context 含 工具结果 + 知识库内容）
```
**全链路降级**：LLM/工具/MCP 任一失败都不崩，回退到无意图/原问题/错误字符串。

## 运行
```bash
pip install -r requirements.txt
# 嵌入层（严格对齐 ragent infra-ai.embedding 家族；默认走 Qwen3-Embedding-8B @ 硅基流动）
cp .env.example .env
# 至少设置 SILICONFLOW_API_KEY（首选）；不设也能降级到 Ollama 本地（EMBEDDING_DEFAULT_MODEL=qwen-emb-local）
# 精排首次运行会下载 bge-reranker-base（约 1.1GB）；国内可设镜像：
#   export SENTENCE_TRANSFORMERS_HOME=$HOME/.cache/sentence-transformers
#   export HF_ENDPOINT=https://hf-mirror.com
export AGENT_LLM_BASE_URL=http://localhost:11434/v1   # Ollama 本地；或云端 OpenAI 兼容
export AGENT_LLM_API_KEY=sk-xxx
export AGENT_LLM_MODEL=qwen2.5:7b
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```
MCP 工具服务（独立进程）：
```bash
cd mcp-server-py && pip install mcp && python server.py --transport sse   # http://127.0.0.1:8000/sse
```

## 接口
- `POST /chat`：`{"question": "..."}` → SSE 流式 `data: {"token": "..."}`
- `POST /ingest`：文件上传或文本 → 返回 chunks（供 Java 侧落库）
- `POST /search`：纯检索，`?query=&top_k=&collections=`，返回 RRF 融合后的命中片段
- `GET /health`

## 测试状态
- 语法检查（py_compile）：全部模块通过
- parser/chunker：Markdown/文本/CSV 解析、block-aware 分块、整文档模式、溯源 ✅
- /ingest HTTP：分块正确、非法扩展名 422 ✅
- agent 编排（mock LLM）：降级链路（LLM 全挂/MCP 失败均不崩）、工具结果入上下文 ✅
- retrieval 引擎：RRF 融合正确、通道失败降级、单通道不融合、预算截断、端到端 /search ✅
- 待真实环境验证：接 Ollama/云端 LLM 跑通 /chat、装 mcp 包后真调工具、装 bge+faiss+rank-bm25 后跑 /search 端到端
