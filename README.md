# Sentinel �?Java 后端 + Python Agent 分层架构

本工作区统一收口两个语言栈，�?*职责边界**做技术选型（详�?`resources/docs/ARCHITECTURE_SPLIT.md`）：

| 目录 | 语言 | 职责 |
|---|---|---|
| `backend-java/` | Java (Spring Boot) | 业务后端骨架：Web 接入、Sa-Token 鉴权、MySQL 持久化、RocketMQ 事务、Redisson 限流、Trace/审计。Java 主体代码位于 `../sentinel-main`，本目录放置�?Python 对接�?*适配�?客户�?*�?|
| `agent-python/` | Python (FastAPI) | Agent 智能层：模型编排、混合检�?RRF/Rerank)、文档解析分块、会话记忆、MCP 工具、RAG 评估�?|
| `resources/` | �?| 已下载的 MITRE ATT&CK / OWASP 材料，供 RAG 知识库使用�?|

## 通信方式
- **HTTP**：`backend-java` 调用 `agent-python` �?FastAPI 接口（`/chat`、`/ingest`、`/search`），单跳调用�?
- **MCP**：`agent-python/mcp-server-py` 作为 MCP Server，Java 端通过 MCP 客户端发�?调用工具�?

## 端口分配（固定，禁止无故更换�?

| 服务 | 端口 | 说明 |
|---|---|---|
| Java 后端（Web�?| 9090 | 业务 API，`/api/sentinel` 上下�?|
| Java 后端（Actuator/management�?| 9099 | 监控端口（如未启用则不监听） |
| Python Agent（FastAPI�?| **8001** | 原设�?8000，因 Docker/WSL 已占�?8000（`com.docker.backend`/`wslrelay`）改�?8001�?*Java �?`sentinel.python.base-url` 必须同步指向 8001** |
| MCP Server（Java 项目 `mcp-server`�?| 9099 | Spring Boot MCP Server，监�?9099�?*Java �?`rag.mcp.servers[0].url` 指向 `http://localhost:9099`（默认值，勿改�?* |
| 前端（Vite�?| 5173 | 开发服务器 |
| Docker | 8000 | 系统占用，不可动；故 Python Agent 改用 8001（MCP 仍在 9099，不受影响） |

> 端口变更原则：非冲突或显式决策，不得更换端口；任一边变更端口，对接侧配置必须同步对齐（Java �?`base-url`↔Python、`mcp.url`↔MCP）�?

## 设计变更记录：Python Agent 端口 8000 �?8001

- **问题**：新增的 `agent-python`（FastAPI）原设计监听 **8000**。但本机 8000 已被 Docker 占用（`com.docker.backend.exe` PID 6228 / `wslrelay.exe` PID 15028，均不可动），导�?Python Agent 无法�?8000 启动，进而后端调�?Python 超时、Agent 不回复�?
- **对比 sentinel-main**：sentinel �?Python 相关服务使用 **9621**（非 8000），且不提供独立 `agent-python`，故未踩此坑。结论：8000 �?端口规划未避开系统占用"的设计失误，�?多占用端�?�?
- **决策**：将 Python Agent 正式设计端口改为 **8001**（可用、不与系统服务冲突）�?
- **对齐动作**�?
  1. `agent-python/core/config.py`：`port` 默认�?8000 �?8001
  2. `agent-python/README.md`：启动命�?`--port 8000` �?`--port 8001`
  3. `backend-java/.../application.yaml`：`sentinel.python.base-url` 默认�?�?`http://localhost:8001`
  4. 重新打包 `bootstrap-0.0.1-SNAPSHOT.jar`，将 8001 固化�?jar（不再依赖运行时环境变量覆盖�?
  5. `backend-java/.../application.yaml`：`rag.mcp.servers[0].url` 保持 `http://localhost:9099`（MCP �?Java 项目，与 8000 冲突无关�?
- **固化结论**：后端启动无需再传 `sentinel_PYTHON_BASE_URL` 环境变量，jar 内已�?8001�?

## 启动流程（顺序：后端 �?MCP �?前端；Agent 层先行）

0. **环境**：JDK 17（`D:\EdgeDownload\zulu17.68.203-ca-jdk17.0.20.1-win_x64`）；Python �?`C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe`�?
1. **Python Agent**：`cd agent-python && python -m uvicorn server:app --host 0.0.0.0 --port 8001`
2. **MCP Server（Java 项目�?*：`cd mcp-server && java -jar target/mcp-server-0.0.1-SNAPSHOT.jar`（监�?9099�?
3. **Java 后端**：`cd backend-java/bootstrap/target && java -jar bootstrap-0.0.1-SNAPSHOT.jar`（无需运行时覆盖，base-url=8001 已固化进 jar�?
4. **前端**：`cd frontend && npm run dev`（Vite�?173�?

> 注：Java 主体代码实际位于本目�?`backend-java`（非 `../sentinel-main`），README 首段旧描述已过时�?

## 设计决策（为什么双语言而非�?Python 重写�?
AI 编排层在 Python 生态（LangChain / ragas / unstructured）工程效率与迭代速度显著优于 Java�?
而业务后端的生产保障�?2 张表持久化、RocketMQ 事务消息、Redisson 公平限流、Sa-Token 审计�?
�?Python 侧无对等物且重写引入风险。故保留 Java 底座、独�?Python agent，按职责分层而非按语言重写�?
