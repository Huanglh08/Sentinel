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

package com.nageoffer.ai.sentinel.rag.core.retrieval.postprocessor;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.nageoffer.ai.sentinel.framework.convention.RetrievedChunk;
import com.nageoffer.ai.sentinel.rag.config.RAGConfigProperties;
import com.nageoffer.ai.sentinel.rag.core.retrieval.channel.SearchChannelResult;
import com.nageoffer.ai.sentinel.rag.core.retrieval.channel.SearchContext;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Objects;

/**
 * 双模型 rerank 相关性门控后置处理器（P1 负样本治理）
 * <p>
 * 负样本（与知识库无关的问题）在向量/全文检索下仍会召回"高分垃圾"chunk，
 * 且 rerank 模型对其相关性打分虚高，下游基于垃圾上下文作答即幻觉。
 * 本处理器位于 {@link RerankPostProcessor}（order=10）之后、元数据富化（order=20）之前，
 * 调用本地双模型打分服务（agent-python {@code /retrieval/rerank-gate}，进程挂 8001 端口）：
 * <ul>
 *   <li>bge-reranker-v2-m3：判别式序列分类，对语义精确匹配敏感</li>
 *   <li>Qwen3-Reranker-0.6B：生成式判官 yes/no logit，对语义泛化敏感</li>
 * </ul>
 * 两个模型的 top1 分数<b>同时</b>低于阈值（AND 门控，刻意保守：单模型低分常见于
 * 表述差异误判，双低才切）时判定"库中大概率无相关信息"，返回空列表 → 上层走
 * "库中无此信息"拒答。
 * <p>
 * 打分对象与离线校准（agent-python eval/rerank_gate_eval.py）一致：Rerank 后的
 * 最终 chunk 前 N 条——离线校准出的阈值可直接用于生产。
 * <p>
 * <b>fail-open 约束</b>：打分服务未启动/超时/返回异常/模型未装载时一律放行原候选，
 * 绝不因门控自身故障制造误拒。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class RelevanceGatePostProcessor implements SearchResultPostProcessor {

    private final RAGConfigProperties ragConfigProperties;

    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3))
            .build();

    private final ObjectMapper objectMapper = new ObjectMapper();

    @Override
    public String getName() {
        return "RelevanceGate";
    }

    @Override
    public int getOrder() {
        return 15;  // Rerank(10) 之后、元数据富化(20) 之前：切空后不再白做富化
    }

    @Override
    public boolean isEnabled(SearchContext context) {
        return Boolean.TRUE.equals(ragConfigProperties.getRetrievalGateEnabled());
    }

    @Override
    public List<RetrievedChunk> process(List<RetrievedChunk> chunks,
                                        List<SearchChannelResult> results,
                                        SearchContext context) {
        if (chunks.isEmpty()) {
            return chunks;
        }
        try {
            GateVerdict verdict = callScoringService(chunks, context.getMainQuestion());
            if (verdict.error != null) {
                log.warn("相关性门控 fail-open（{}），放行 {} 条 chunk", verdict.error, chunks.size());
                return chunks;
            }
            log.info("相关性门控 - bge_top1={} qwen3_top1={} 阈值=({}, {}) scored={} 触发={}",
                    String.format("%.4f", verdict.bgeTop1), String.format("%.4f", verdict.qwen3Top1),
                    ragConfigProperties.getRetrievalGateBgeThreshold(),
                    ragConfigProperties.getRetrievalGateQwen3Threshold(),
                    verdict.scored, verdict.triggered);
            if (verdict.triggered) {
                log.warn("相关性门控触发，切空 {} 条 chunk，走无知识拒答（题目: {}）",
                        chunks.size(), abbreviate(context.getMainQuestion()));
                return List.of();
            }
            return chunks;
        } catch (Exception e) {
            log.warn("相关性门控调用异常 fail-open，放行 {} 条 chunk", chunks.size(), e);
            return chunks;
        }
    }

    /**
     * 调用 agent-python 双模型打分服务，解析触发判定
     */
    private GateVerdict callScoringService(List<RetrievedChunk> chunks, String question) throws Exception {
        ObjectNode request = objectMapper.createObjectNode();
        request.put("question", question);
        ArrayNode texts = request.putArray("texts");
        chunks.stream()
                .limit(ragConfigProperties.getRetrievalGateTopN())
                .map(RetrievedChunk::getText)
                .filter(Objects::nonNull)
                .forEach(texts::add);
        request.put("bge_threshold", ragConfigProperties.getRetrievalGateBgeThreshold());
        request.put("qwen3_threshold", ragConfigProperties.getRetrievalGateQwen3Threshold());

        HttpRequest httpRequest = HttpRequest.newBuilder()
                .uri(URI.create(ragConfigProperties.getRetrievalGateUrl()))
                .timeout(Duration.ofMillis(ragConfigProperties.getRetrievalGateTimeoutMs()))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(request)))
                .build();

        HttpResponse<String> response = httpClient.send(httpRequest, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            return GateVerdict.error("HTTP " + response.statusCode());
        }
        JsonNode body = objectMapper.readTree(response.body());
        GateVerdict verdict = new GateVerdict();
        verdict.triggered = body.path("triggered").asBoolean(false);
        verdict.bgeTop1 = body.path("bge_top1").asDouble(0.0);
        verdict.qwen3Top1 = body.path("qwen3_top1").asDouble(0.0);
        verdict.scored = body.path("scored").asInt(0);
        verdict.error = body.path("error").isNull() || body.path("error").isMissingNode()
                ? null : body.path("error").asText();
        if (verdict.scored <= 0 && verdict.error == null) {
            return GateVerdict.error("empty scores");
        }
        return verdict;
    }

    private static String abbreviate(String text) {
        if (text == null) {
            return "";
        }
        return text.length() <= 50 ? text : text.substring(0, 50) + "...";
    }

    /**
     * 门控判定结果（内部承载，error 非 null 表示 fail-open）
     */
    private static class GateVerdict {
        boolean triggered;
        double bgeTop1;
        double qwen3Top1;
        int scored;
        String error;

        static GateVerdict error(String message) {
            GateVerdict verdict = new GateVerdict();
            verdict.error = message;
            return verdict;
        }
    }
}
