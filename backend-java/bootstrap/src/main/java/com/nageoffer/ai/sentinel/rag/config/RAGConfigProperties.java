/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.nageoffer.ai.sentinel.rag.config;

import lombok.Data;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;

/**
 * RAG 系统功能配置
 *
 * <p>
 * 用于管理 RAG 系统的各项功能开关，例如查询重写等
 * </p>
 *
 * <pre>
 * 示例配置：
 *
 * rag:
 *   query-rewrite:
 *     enabled: true
 * </pre>
 */
@Data
@Configuration
public class RAGConfigProperties {

    /**
     * 查询重写功能开关
     * <p>
     * 控制是否启用查询重写功能，查询重写可以将用户的查询语句优化为更适合检索的形式
     * 默认值：{@code true}
     */
    @Value("${rag.query-rewrite.enabled:true}")
    private Boolean queryRewriteEnabled;

    /**
     * Rerank 重排序功能开关
     * <p>
     * 控制是否启用 Rerank 后置处理器对召回结果进行重排序
     * 默认值：{@code true}
     */
    @Value("${rag.rerank.enabled:true}")
    private Boolean rerankEnabled;

    /**
     * 上下文元数据富化开关
     * <p>
     * 控制是否在检索末端回表补齐 chunk 的文档归属信息（文档ID/序号/标题），
     * 并在组装上下文时按文档聚合、组内按序号排列、带上文档标题作为内部锚点
     * 关闭后组装退回按检索相关性平铺、不带来源
     * 默认值：{@code true}
     */
    @Value("${rag.context.enrich.enabled:true}")
    private Boolean contextEnrichEnabled;

    /**
     * 回答行内引用（数字角标）开关
     * <p>
     * 开启后为进入上下文的资料注入请求级引用编号，并动态追加行内引用规则，模型在正文末尾输出 {@code [N](#cite-N)}
     * 关闭则不注入编号、不追加行内引用规则，省下这部分系统提示词与上下文开销以降低首字延迟；
     * 文档级来源面板由 SSE 单独下发，不受此开关影响
     * 默认值：{@code false}
     */
    @Value("${rag.citation.enabled:false}")
    private Boolean citationEnabled;

    /**
     * 双模型 rerank 相关性门控开关（P1 负样本治理）
     * <p>
     * 开启后由 {@code RelevanceGatePostProcessor}（Rerank 之后）调用本地双模型打分服务
     * （agent-python /retrieval/rerank-gate），当 bge 与 qwen3 两个模型的 top1 分数
     * 同时低于阈值（AND 门控）时判定"库中大概率无相关信息"，切空检索结果走拒答。
     * 打分服务不可用/超时/异常时 fail-open 放行，绝不因门控故障误拒
     * 默认值：{@code true}（fail-open 语义下开启无风险）
     */
    @Value("${rag.retrieval-gate.enabled:true}")
    private Boolean retrievalGateEnabled;

    /**
     * 双模型门控打分服务地址（agent-python FastAPI）
     */
    @Value("${rag.retrieval-gate.url:http://127.0.0.1:8001/retrieval/rerank-gate}")
    private String retrievalGateUrl;

    /**
     * 打分服务 HTTP 超时（毫秒）；超时按 fail-open 放行
     */
    @Value("${rag.retrieval-gate.timeout-ms:5000}")
    private Long retrievalGateTimeoutMs;

    /**
     * bge-reranker-v2-m3 的 top1 切空阈值（sigmoid 概率）
     */
    @Value("${rag.retrieval-gate.bge-threshold:0.10}")
    private Double retrievalGateBgeThreshold;

    /**
     * Qwen3-Reranker-0.6B 的 top1 切空阈值（sigmoid(yes-no) 概率）
     */
    @Value("${rag.retrieval-gate.qwen3-threshold:0.20}")
    private Double retrievalGateQwen3Threshold;

    /**
     * 参与打分的最大 chunk 数（取 Rerank 后前 N 条，门控只看 top1）
     */
    @Value("${rag.retrieval-gate.top-n:8}")
    private Integer retrievalGateTopN;

    /**
     * 门控触发后的处置模式（P2 软着陆，对齐 Azure AI Search "检索宽进、生成层验证" 模式）
     * <p>
     * true：触发时不再切空，保留 top-K chunk 交由生成层按 grounding 规则
     * 判定"资料未覆盖则明确说明"——消除切空对正样本的误伤（离线校准 12.1%）。
     * false：切空检索结果走无知识拒答（旧行为）。
     */
    @Value("${rag.retrieval-gate.soft-landing:true}")
    private Boolean retrievalGateSoftLanding;

    /**
     * 软着陆模式下保留的最大 chunk 数（取 Rerank 后前 N 条，弱相关垃圾不全部透传）
     */
    @Value("${rag.retrieval-gate.soft-landing-top-k:5}")
    private Integer retrievalGateSoftLandingTopK;
}
