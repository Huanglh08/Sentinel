<p align="center">
  <a href="https://github.com/nageoffer/sentinel">
    <picture>
      <source srcset="assets/sentinel-ai-banner.png">
      <img src="assets/sentinel-ai-banner.png" alt="sentinel AI">
    </picture>
  </a>
</p>

<p align="center">
  <strong>后端程序员转�?AI 工程师的第一�?/strong><br/>
</p>

<p align="center">
  <a href="https://github.com/nageoffer/sentinel/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/nageoffer/sentinel?style=flat-square&logo=github&color=e8b227" /></a>&nbsp;
  <a href="https://github.com/nageoffer/sentinel/network/members"><img alt="GitHub forks" src="https://img.shields.io/github/forks/nageoffer/sentinel?style=flat-square&logo=github&color=2d6a8a" /></a>&nbsp;
  <a href="https://github.com/nageoffer/sentinel/graphs/contributors"><img alt="Contributors" src="https://img.shields.io/github/contributors/nageoffer/sentinel?style=flat-square&color=b56e7a" /></a>&nbsp;
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-4a9b8f?style=flat-square" /></a>
  <img src="https://img.shields.io/badge/Spring%20AI-2.0-6DB33F?style=flat-square&logo=springboot&logoColor=white" />
</p>

## 🚀 什么是 sentinel AI�?
sentinel 是一个面�?Agentic RAG 演进的生产级 Java AI 应用平台，覆盖从文档入库到智能问答的完整链路�?
- **混合检�?*：向量、关键词、知识图谱、联网搜索并行召回，支持去重、RRF 融合�?Rerank�?- **问题理解**：支持查询词映射、问题重写与拆分、树形意图识别和多知识库路由�?- **模型与工�?*：支持模型档位、首包探测、熔断降级，以及 MCP 工具发现、提参与校验�?- **会话记忆**：最�?N 轮消息结合持久化摘要，控�?Token 成本并保留关键上下文�?- **流量保护**：Redis 公平排队与分布式并发控制，避免突发请求压垮模型服务�?- **知识闭环**：提供可编排入库 Pipeline、远程刷新、回答溯源、用户反馈、Trace 和管理后台�?
生产落地智能体会踩的坑，这里都有对应方案，一套经过真实场景锤炼的工程实践，系统补�?RAG / Agent / MCP 等知识，面试写进简历聊得起来�?
![](assets/sentinel-framework.png)

## 🧭 快速导�?
> 觉得不错？先点个 Star 收藏，所有链接都会在新标签页打开，不会离开本页 👇

<p align="left">
  <a href="https://github.com/nageoffer/sentinel/stargazers">
    <img src="assets/star-me.svg" alt="Star this repo" />
  </a>
</p>

| &nbsp; | 链接 | 说明 |
|:---:|:---|:---|
| 📖 | <a href="https://nageoffer.com/sentinel" target="_blank">官网文档</a> | sentinel AI 完整文档 |
| 馃殌 | <a href="https://nageoffer.com/sentinel/demo/" target="_blank">鍦ㄧ嚎浣撻獙</a> | 鏃犻渶閮ㄧ讲锛岀洿鎺ヤ綋楠?sentinel AI |
| �?| <a href="https://nageoffer.com/sentinel/local-dev/" target="_blank">快速启�?/a> | 本地搭建 sentinel AI 前后端项�?|
| 📝 | <a href="https://nageoffer.com/sentinel/interview/" target="_blank">简历怎么�?/a> | 项目如何写到简历上 |
| 💡 | <a href="https://nageoffer.com/sentinel/why-springai-langchain4j/" target="_blank">为什么不�?Spring AI / LangChain4j</a> | 技术选型的思�?|

## 🤝 贡献

sentinel AI 仍在持续迭代中，欢迎参与共建，一起把项目打磨得更好�?感谢各位亦菲、彦祖们�?sentinel AI 的贡献：

<p align="left">
    <a href="https://github.com/nageoffer/sentinel/graphs/contributors">
        <img src="https://contrib.rocks/image?repo=nageoffer/sentinel&columns=8" />
    </a>
</p>

## 鉂わ笍 璧炲姪涓庢敮鎸?
鎰熻阿浠ヤ笅浼欎即瀵?sentinel AI 鐨勬敮鎸侊紒

<table>
  <tr>
    <td align="center" width="200">
      <a href="https://moacode.org/register?ref=doorbyte"><img src="assets/moacode.svg" alt="moacode" width="200" /></a>
    </td>
    <td>
      moacode 是一家稳定、高效的 API 中转服务商，提供 Claude、GPT、Codex、Claude Code 等多种模型调用服务。Sentinel AI 用户�?a href="https://moacode.org/register?ref=doorbyte">通过此链�?/a>注册，并在充值时填写 <code>MAGE</code> 优惠码，享受 9 折优惠�?    </td>
  </tr>
</table>

---

## 💡 为什么学�?AI 项目

AI 这波浪潮，Java 程序员已经躲不过去了�?
不管你现在做的是业务系统还是中间件，面试的时候多多少少都会被问到 AI 相关的东西。RAG 是什么？Agent 怎么实现？用�?MCP 吗？这些问题越来越高频。可以说�?*AI 已经从加分项变成了必答题**�?
但说实话，对于大多数应用层的开发者来说，去死磕大模型的微调、蒸馏、Transformer 原理，性价比并不高。真正实用的，是掌握 RAG �?Agent 这些应用层的东西——能落地、能出活、面试也能聊得起来�?
<details>
<summary><b>校招 / 社招 / 怎么学？</b>（点击展开�?/summary>

### 1. 校招现状

简历上清一色的 CRUD 项目——商城、外卖、博客，面试官早就审美疲劳了。当别人还在写基�?SpringBoot �?XX 管理系统时，你简历上有一个完整的 AI 项目，区分度直接拉满。而且大厂校招越来越看重候选人对新技术的敏感度，AI 项目能直接证明你的学习能力和技术视野�?
### 2. 社招现状

2024 年以来，几乎所有技术团队都在往 AI 方向靠。很多公司已经把�?AI 相关经验写进�?JD 里。你可能 Java/Go 写得很溜，但面试官会问：你对 LLM 了解多少？RAG 做过没有？向量检索怎么实现的？答不上来，直接少了一个谈薪的筹码�?
**璇寸櫧浜嗭紝瀛?AI 椤圭洰鐨勬牳蹇冨師鍥犲氨涓変釜锛?*

1. **简历差异化**。同样是后端开发，�?AI 项目经验的简历通过率明显更高。不是因�?AI 多神奇，而是它能证明你不只是在重复造轮子�?2. **面试有东西聊**。AI 项目涉及的技术栈足够深——Embedding、向量数据库、Prompt 工程、模型调用链路、检索策略……每一个点都能展开聊，比我用了 Redis 做缓存有意思得多�?3. **实际工作用得�?*。AI 不是实验室里的玩具，企业已经在大规模落地了。现在学，是为了接下来三到五年的职业发展铺路�?
### 3. 问题是，怎么学？

很多人跟着 B 站视频或�?GitHub 上的开源项目撸了一遍，以为自己懂了。结果面试一问深的，直接懵了。原因很简单：那些 Demo 级别的项目，和企业真正要用的东西，差距太大了�?
还有些同学报了训练营，发现清一色是 Python。语言不熟、生态不通，学完感觉收获有限，回�?Java 这边还是不知道怎么下手。就算用 Spring AI 或�?LangChain4j，版本迭代太快，低版本功能缺，高版本升级约等于重写，也是一肚子苦水�?
基于这些问题，我决定做一�?RAG 实战项目，名字叫 **sentinel**�?
这个项目会覆盖市面上主流�?RAG 技术点，也会涉�?MCP、Agent 等场景。更重要的是，它不是我看了几篇文章拼凑出来的玩具——我在公�?*实际落地�?RAG 系统**，解决过信息孤岛、知识检索、效率提升这些真实的业务问题。所�?sentinel 的复杂度，就是企业级项目该有的复杂度�?
学完之后，你可以放心大胆地跟面试官讲�?*企业里就是这么做�?*�?
</details>

## ⚠️ RAG 常见误区

市面上打着 RAG 旗号的项目不少，但很多要么是玩具�?Demo，要么是概念包装。在学之前，先把这几个误区理清楚，避免踩坑�?
常规 RAG 的流程如下：

![](assets/rag-misconceptions-v2.png)

<details>
<summary><b>4 个常见误区详�?/b>（点击展开�?/summary>

### 1. 璋冧釜 API 灏辩畻浼?RAG 浜?
很多教程的套路是：调一�?OpenAI �?Embedding 接口，往向量数据库里塞点数据，再�?LLM 生成答案——完事了。这顶多算跑通了一�?Demo，离�?RAG 差得远�?
真正�?RAG 系统要考虑的问题多得多：文档怎么切分效果最好？检索召回率不够怎么办？多路召回怎么融合排序？幻觉怎么控制？这些才是面试官会追问的点�?
跑�?Demo 和做出能上线的系统之间，差的不是代码量，是对每个环节的深入理解�?
### 2. RAG 就是“检�?+ 生成”两步走

`Retrieval-Augmented Generation` 这个名字确实容易让人觉得就是检索加生成。但实际工程中，一个能用的 RAG 系统至少涉及这些环节�?
- **数据处理**：PDF、Word、PPT、网页，格式五花八门，光是解析成干净文本就是一堆脏活。PDF 里的表格、扫描件、双栏排版，每一个都是坑�?- **分块策略**：切太大检索不精准，切太小上下文丢失。按段落切、按固定字数切、按语义切，不同文档可能需要不同策略�?- **问题重写**：用户问“报销咋整”，你拿这四个字去检索，效果能好吗？多轮对话里用户说“怎么申请”，不补上下文系统根本不知道在问啥�?- **意图识别**：用户是想查知识库，还是要调用业务系统？是闲聊还是正经提问？走错了路，答案肯定不对�?- **检索策�?*：纯向量检索对精确匹配很弱，用户问一个订单号，向量检索可能完全找不到。混合检索怎么融合、top-k 选多少、要不要重排序，都是取舍�?- **会话记忆**�?0 轮对话全塞给模型？Token 成本扛不住。只带最近几轮？可能丢关键上下文。记忆的压缩、摘要、持久化，又是一套单独的机制�?
每一环都有坑，每一环都值得深挖。面试的时候能把这些讲清楚，比背概念有用得多�?
### 3. �?OpenAI/LangChain 套一套就是企业级

OpenAI/LangChain 是个好工具，但直接拿来套壳不等于企业级。企业场景下要面对的是：

- 大规模文档的增量更新，不可能每次全量重建索引
- 多租户隔离和权限控制，不同部门看到的知识库不一�?- 高并发下的检索性能，模型调用的成本控制和容�?- 请求风控，防止用户套取敏感信息或恶意攻击
- 模型负载均衡，多供应商切换和降级策略
- 可观测性，效果监控和用户反馈收�?
这些问题 OpenAI/LangChain �?QuickStart 不会告诉你，但面试官和实际业务一定会考你�?
### 4. 只关注模型，忽略工程能力

RAG 项目的核心竞争力不在于你用了多强的模型，而在于工程化能力。同样的模型，检索策略不同、Prompt 设计不同、分块粒度不同，最终效果可以天差地别�?
举个例子：用户问“打印机墨盒怎么换”，文档里写的是“墨盒更换步骤”。关键词搜索直接匹配不上，但向量检索能理解它们是一回事。这背后�?Embedding 模型的选型、向量数据库的调优、检索结果的重排序——每一步都是工程决策，不是换个更贵的模型就能解决的�?
面试中能把这些工程细节讲清楚的人，远比只会说"我用�?GPT-4"的人有说服力�?
</details>

## 🏗�?sentinel 核心设计

采用前后端分离的模块化单体架构，后端按职责分为三个 Maven 模块：�?
| 模块 | 职责 |
|:---|:---|
| `framework` | 统一响应与异常、认证上下文、幂等、分布式 ID、MQ 适配、Trace 与 SSE 等通用基础能力 |
| `infra-ai` | Chat / Embedding / Rerank / VLM 模型客户端、模型档位、路由、首包探测、健康状态与降级 |
| `bootstrap` | RAG 问答、知识库、入库 Pipeline、意图树、检索、会话、审计及管理端 API |

这个分层不是为了炫技，而是把业务编排、AI 供应商差异和通用基础设施隔离开。切换模型、向量库或对象存储时，核心问答流程不需要跟着重写�?
![](assets/sentinel-module-layering-v2.png)

一次用户提问，�?sentinel AI 服务里经过的核心链路如下所示：

> 实际项目代码中，逻辑比图表上更加复杂。下图仅展示核心流程，落地过程中还涉及很多细节和优化�?
![](assets/sentinel-chain-v3.png)

<details>
<summary><b>多路检索架构、模型路由与容错�?/b>（点击展开�?/summary>

检索是 RAG 系统的核心。Sentinel 当前提供向量、Elasticsearch 关键词、LightRAG 知识图谱�?You.com 联网搜索四类通道，按配置启用后并行执行：

![](assets/multi-channel-retrieval.png)

每个通道独立执行、互不影响，通过专用线程池并行调度。后处理链依次完成去重、加�?RRF 融合、Rerank 和元数据富化；召回预算、Rerank 候选池与最终上下文条数分段配置，并在启动时校验漏斗不变式�?
生产环境不可能只依赖一个模型供应商，Sentinel 的模型路由机制解决的就是这个问题�?
![](assets/model-routing-failover.svg)

关键设计：三态熔断器，用于保护系统不会持续调用已经故障的模型�?
![](assets/model-health-store.svg)

文档从上传到可检索，经过一条基于节点编排的 Pipeline�?
<img src="assets/ingestion-pipeline.png" width="25%" />

每个节点的配置存储在数据库中，支持条件执行和输出链式传递。每个任务和节点都有独立的执行日志，出了问题能精确定位到哪一步�?
sentinel 不是为了用设计模式而用，每个模式都对应一个具体的工程问题�?
| 设计方式 | 业务场景 | 解决的问�?|
|:---|:---|:---|
| 策略 | 检索通道、结果后处理、文档来�?| 不同实现可独立替�?|
| 工厂 | 意图树、分块策略、流式回调创�?| 集中复杂对象的创建逻辑 |
| 模板方法 | 并行检索、模型请�?| 固定通用流程，仅开放差异步�?|
| 注册�?| MCP 工具发现、意图节点管�?| 统一注册、查找和调用组件 |
| 装饰�?| 向量写入时同步关键词和图谱索�?| 在不修改主流程的前提下增强能�?|
| 责任�?| 检索后处理、模型故障降�?| 按顺序组合处理步�?|
| 事件回调 | 模型流式响应、首包探测、SSE 输出 | 解耦事件生产与消费 |
| AOP | 链路追踪、幂等、审计日�?| 将横切逻辑与业务解�?|

</details>

## 鉁?椤圭洰璐ㄩ噺鎬庝箞鏍凤紵

这里的质量不靠一张架构图来证明，而是看代码边界、测试、故障处理和运维闭环是否真实存在。以下数据按当前仓库统计，代码行数包含注释和空行�?
### 1. 规模与完整度

- **后端**�? �?Maven 模块，约 **6.1** 万行 Java 主代码�?53 个主代码文件�?- **前端**：约 **2.75** 万行代码�?7 个页面级 TSX 文件�?- **数据与测�?*�?*22** 张业务表�?0 �?Java 测试文件�?4 �?`@Test` 测试点�?
代码量本身不等于质量，但这些模块组成了数据进入系统—检索生成答案—展示证据—收集反馈—追踪与审计的完整业务闭环，不仅是简单的 API 示例�?
### 2. 工程质量

- **模块边界**：通用基础设施、AI 能力、RAG 业务�?MCP 服务相互隔离，替换模型或存储实现不会侵入问答编排�?- **配置防错**：模型档位、候选能力和检索漏斗在启动阶段完成一致性校验，错误配置直接失败而不是静默降质�?- **并发治理**�?0 个专用线程池隔离负载，TTL 保证用户�?Trace 上下文跨线程传递�?- **关键路径测试**：覆盖模型路由、检索预算、结果去重、会话摘要、入�?Pipeline �?MCP�?- **工程约束**：统一响应、错误码和异常处理，认证、幂等、线程安�?SSE �?Spotless 格式化均已落到代码�?
> 项目中大量应用并发线程，建议配合社群里的 [oneThread 动态线程池框架](https://nageoffer.com/onethread) 搭配学习收获更多�?
### 3. 可扩展�?
核心能力通过接口、注册表和配置隔离，新增实现可以复用现有编排、容错、日志和管理能力�?
| 扩展维度 | 如何接入 | 接入后的效果 |
|:---|:---|:---|
| 模型 | 实现 `ChatClient` / `EmbeddingClient` / `RerankClient`，加入模型候选配�?| 新供应商可进入模型档位与候选路由，复用首包探测、健康检查和熔断降级 |
| 存储 | 实现向量存取�?`ObjectStorageClient`，通过配置选择实现 | 可替换向量库或对象存储，知识入库与问答主流程保持不变 |
| 检�?| 实现 `SearchChannel` 或后处理器，注册�?Spring Bean 并设置顺�?| 新通道参与并行召回，新处理器可插入去重、融合、精排与富化链路 |
| 入库 | 实现 `IngestionNode` �?`DocumentFetcher`，补充节点类型和配置 | 新处理步骤或文档来源进入 Pipeline，继续使用任务状态、节点日志和失败定位 |
| MCP | 暴露 MCP 工具规范，或在客户端配置外部 MCP Server | 工具可被远程发现，并复用参数提取、Schema 校验与调用流�?|

扩展的改动主要收敛在新实现和配置中，不必复制一套检索、会话或 Trace 主链路�?
### 4. 鐢熶骇绾х壒鎬?
这里的生产级特性指项目已经实现生产环境会遇到的关键机制，而不只是功能能跑�?
| 特�?| 说明 |
|:---|:---|
| **流量保护** | Redis ZSET 公平排队，结�?Lua 原子抢占、过期信号量�?Pub/Sub 唤醒；支持进度、超时、取消与许可回收 |
| **模型容错** | 多候选模型自动切换，首包超时、空响应或异常时触发降级；三态熔断隔离故障节�?|
| **检索稳定�?* | 多通道并行检索，单通道失败不影响主链；按召回、Rerank、TopK 分层控制检索规�?|
| **数据一致�?* | RocketMQ 事务消息保障分块与删除任务可靠执行；关键操作支持幂等，远程刷新使用分布式锁和状态机 |
| **可观测与审计** | 记录 Trace Run / Node 的耗时、输入输出和异常；管理端提供趋势、详情及配置变更 Diff |
| **流式体验** | SSE 分事件输出思考、正文、来源和推荐问题；支持全局超时及客户端断开取消 |
| **会话与证�?* | 最�?N 轮消息结合持久化摘要控制上下文；保存引用来源�?Grounding Chunk，支持原文预览、追问和反馈 |
| **安全基础** | Sa-Token 认证、数据归属校验、上传限流、参数校验和统一异常处理；敏感配置支持环境变量注�?|

### 5. 瀹屾暣鎺у埗鍙?
sentinel 提供覆盖**普通用户与管理员用�?*�?React 控制台，不只是聊天页面，也把检索证据和运维入口暴露出来�?
系统通过多轮 AI 辅助设计优化，在保证功能完整性的同时，提供更加现代化和友好的交互体验�?
#### 5.1 用户问答界面

用户访问 sentinel 首页后，可在输入框中直接输入问题发起问答，同时支持开�?*深度思考模�?*以获得更高质量的回答�?
输入框下方提供示例问题标签，用户点击即可自动填充问题，方便快速体验系统能力�?
- 支持自然语言输入
- 支持示例问题快速填�?- 支持深度思考模�?
![](assets/qa-home.png)

用户提交问题后，模型会实时生成回答结果，并提供良好的阅读体验�?
- 支持 Markdown 格式渲染
- 支持图片内容展示
- 支持代码高亮显示
- 支持回答来源、原文预览和推荐追问
- 支持回答评价（点�?/ 点踩�?
![](assets/qa-answer.png)

#### 5.2 管理后台

管理员可以通过后台查看仪表盘，管理知识库与 Chunk、知识图谱、意图树、查询词映射、入库任务、示例问题、用户和系统设置，并查看 RAG Trace 与业务变更日志�?
<details>
<summary><b>管理后台界面截图</b>（点击展开�?/summary>

![](assets/admin-overview.png)

![](assets/admin-settings.png)

![](assets/admin-knowledge-base.png)

![](assets/admin-datasets.png)

![](assets/admin-trace.png)

![](assets/admin-models.png)

</details>

为了避免传统系统常见的毛坯界面体验，sentinel 的控制台经过多轮 AI 辅助设计与优化，逐步迭代完善，最终呈现出当前简洁、美观且实用的界面效果�?
![](assets/admin-theme.png)

### 6. 和市面上项目的区�?
sentinel 定位�?**Java AI 应用的源码级工程参�?*，重点是完整链路、生产保障和二次开发能力�?
| 对比维度 | 常见 RAG 教程 / Demo | sentinel |
|:---|:-----------------|:---|
| 项目定位 | 跑通检索与生成          | 完整 Java AI 应用 |
| 检�?| 单路向量 TopK        | 向量 / 关键�?/ 图谱 / 联网召回，RRF 融合�?Rerank |
| 问题理解 | 原问题直接检�?         | 查询词映射、问题重写与拆分、树形意图和多知识库路由 |
| 模型调用 | 单模型直�?           | 模型档位、首包探测与熔断降级 |
| 工具接入 | 以应用内函数调用为主 | MCP 协议、远程工具发现与 Schema 校验 |
| 知识入库 | 一次性脚�?           | 可编�?Pipeline、节点日志、远程文档定时刷�?|
| 会话记忆 | 以近期消息拼接为�?| 最�?N 轮消�?+ 持久化摘�?|
| 回答可信�?| 只展示答�?           | 来源引用、原文预览与用户反馈 |
| 运行保障 | 基础日志             | 分布式限流、幂等、事务消息、Trace 与审�?|
| 二次开�?| 流程写死             | 模型、存储、检索、入库和 MCP 均提供扩展接�?|
| 管理能力 | 无或简单页�?          | 完整用户端与管理后台 |

## �?常见问题答疑

一句话：学�?sentinel，你既能跟面试官�?RAG/Agent 的技术深度，也能证明自己�?Java 工程化水平�?
<details>
<summary><b>能学到什�?/ 适合谁？</b>（点击展开�?/summary>

### 1. 能够学到什么？

sentinel 不只是教你调 API，而是让你理解一�?RAG 系统�?0 �?1 落地的全过程。粗略来说，你能收获这些�?
- **RAG 全链路工程能�?*：文档解析、分块策略、Embedding 向量化、多路检索、重排序、Prompt 组装、流式生成，每个环节怎么做、为什么这么做�?- **AI 应用架构设计**：意图识别体系、问题重写与拆分、会话记忆管理、MCP 工具调用，这些是 AI 应用区别于传�?CRUD 系统的核心能力�?- **模型工程化实�?*：模型档位、多候选路由、首包探测、熔断降级，解决模型不稳定时如何保障服务可用性�?- **高质�?Java 工程能力**：分层架构、设计模式实战、分布式并发限流、多线程池管理与上下文透传、全链路追踪，这些能力不局限于 AI 项目，放到任�?Java 后端岗位都是加分项�?- **前后端完整项目经�?*：后�?Spring Boot 3 + 前端 React 18，从 API 设计到页面交互，完整的全栈项目经历�?
### 2. 适合人群

**校招同学�?*

- **Java 后端方向的在校生**：简历上已经有了商城、外卖等常规项目，需要一个有区分度的项目来拉开差距。Sentinel 能让你在面试中聊 AI + 工程化，而不是千篇一律的 CRUD�?- **想转 AI 应用方向的同�?*：对大模型感兴趣，但不想�?Python 和算法入手。Sentinel 基于 Java 技术栈，学习曲线平滑，不需要额外切换语言生态�?- **准备实习/秋招/春招的同�?*：大厂校招越来越看重候选人对新技术的敏感度，简历上�?AI 项目经验，能直接证明你的学习能力和技术视野�?
**社招同学�?*

- **1-3 年经验的 Java 开�?*：日常写业务代码，想往 AI 方向转型但不知道从哪下手。Sentinel 的技术栈你都熟悉，学的是 AI 应用层的东西，上手快、能落地�?- **3-5 年经验的后端开�?*：技术能力不差，但面试被问到 AI 相关问题答不上来，少了一个谈薪筹码。通过 sentinel 补上 RAG、Agent、MCP 这些知识点，面试时能聊得有深度�?- **想跳槽到 AI 团队的开发�?*：越来越多的 JD 要求�?AI 相关经验，Sentinel 能帮你快速建�?RAG 系统的全局认知，面试时不再只是纸上谈兵�?
</details>


## 🌟 为什么开源？

原因很简单：**对项目质量足够自�?*。架构设计、代码实现、工程规范，每一行都经得起审视。好不好�?clone 下来自己看——目录结构、提交记录、注释规范，全是明牌�?
<details>
<summary><b>开源背景与价�?/b>（点击展开�?/summary>

之前做拿�?offer 社群时，第一个业务系�?12306 选择了开源，收获�?<a href="https://github.com/nageoffer/12306"><img src="https://img.shields.io/github/stars/nageoffer/12306?style=flat-square&logo=github&label=GitHub" style="vertical-align: middle;" /></a>
<a href="https://gitee.com/nageoffer/12306/stargazers"><img src="https://gitee.com/nageoffer/12306/badge/star.svg?theme=dark" style="vertical-align: middle;" /></a>，也得到了很多同学的认可和信任。这�?sentinel 作为社群�?AI 领域的第一个项目，同样选择开源——既然代码质量经得起检验，就没必要藏着掖着�?
市面上不少项目只敢放几张截图、讲几个概念，真正敢把代码全部摊开的并不多。Sentinel 敢这么做，是因为前面讲的那些能力——多路检索、意图识别、模型容错、全链路追踪——不�?PPT 里的架构图，是你能跑起来、能断点调试、能逐行阅读的真实代码�?
开源对你来说意味着什么：

- **源码即文�?*：想了解某个模块怎么实现的，直接翻代码，比任何教程都准确、都及时�?- **本地可调�?*：断点打到任意一行，跟着一次请求走完整�?RAG 链路，比看架构图理解得深十倍�?- **可参与贡�?*：发�?Bug �?Issue，有优化思路�?PR。参与一个企业级 AI 开源项目，本身就是简历上的亮点�?- **持续迭代更新**：项目会持续演进，Star �?Watch 之后能第一时间获取新特性�?
</details>

<p align="center">
  <a href="https://www.star-history.com/?repos=nageoffer%2Fsentinel&type=date&legend=top-left">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=nageoffer/sentinel&type=date&theme=dark&legend=top-left" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=nageoffer/sentinel&type=date&legend=top-left" />
      <img alt="Star History Chart" src="https://api.star-history.com/image?repos=nageoffer/sentinel&type=date&legend=top-left" />
    </picture>
  </a>
</p>

<p align="center">
  如果屏幕前的亦菲/彦祖觉得项目还不错，点个 Star 支持一下，这是对开源作者最好的认可�?</p>
