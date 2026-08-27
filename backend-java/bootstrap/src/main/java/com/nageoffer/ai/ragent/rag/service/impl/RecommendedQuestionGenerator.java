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

package com.nageoffer.ai.ragent.rag.service.impl;

import cn.hutool.core.collection.CollUtil;
import cn.hutool.core.util.StrUtil;
import com.nageoffer.ai.ragent.framework.convention.GroundingChunk;
import com.nageoffer.ai.ragent.framework.trace.RagTraceNode;
import com.nageoffer.ai.ragent.rag.client.PythonAgentClient;
import com.nageoffer.ai.ragent.rag.dto.RecommendedQuestionsPayload;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 推荐追问问题生成器
 * <p>
 * 按 g2 分层职责，推荐追问改为调用 Python agent 的 {@code /recommend} 接口，
 * 不再在 Java 侧直连 LLM。失败降级为 {@link RecommendedQuestionsPayload#failed()}。
 * <p>
 * 拆为独立 bean 是为了让 Spring AOP 的 {@link RagTraceNode} 拦截生效（同类 self-call 不触发 proxy）
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class RecommendedQuestionGenerator {

    private static final int DEFAULT_RECOMMEND_COUNT = 3;
    private static final int MAX_QUESTION_CHARS = 1000;
    private static final int MAX_ANSWER_CHARS = 6000;
    private static final int MAX_CHUNKS_CHARS = 6000;
    private static final int MAX_QUESTION_ITEM_CHARS = 200;

    private final PythonAgentClient pythonAgentClient;

    @RagTraceNode(name = "recommended-question-gen", type = "RECOMMEND_GEN")
    public RecommendedQuestionsPayload generate(String question, String answer, List<GroundingChunk> chunks) {
        try {
            List<Map<String, String>> chunkMaps = buildChunks(chunks);
            List<String> questions = pythonAgentClient.recommend(
                    StrUtil.subPre(StrUtil.nullToEmpty(question), MAX_QUESTION_CHARS),
                    StrUtil.subPre(StrUtil.nullToEmpty(answer), MAX_ANSWER_CHARS),
                    chunkMaps
            );
            if (questions == null) {
                return RecommendedQuestionsPayload.failed();
            }
            if (questions.isEmpty()) {
                return RecommendedQuestionsPayload.empty();
            }
            List<String> result = new ArrayList<>();
            for (String q : questions) {
                String text = StrUtil.subPre(StrUtil.trim(q), MAX_QUESTION_ITEM_CHARS);
                if (StrUtil.isNotBlank(text) && !result.contains(text)) {
                    result.add(text);
                }
                if (result.size() >= DEFAULT_RECOMMEND_COUNT) {
                    break;
                }
            }
            if (result.isEmpty()) {
                return RecommendedQuestionsPayload.empty();
            }
            return RecommendedQuestionsPayload.success(result);
        } catch (Exception ex) {
            log.warn("生成推荐追问问题失败", ex);
            return RecommendedQuestionsPayload.failed();
        }
    }

    /**
     * 把 grounding 片段转为 Python /recommend 期望的 {@code [{doc_name, text}]} 结构，并按预算截断
     */
    private List<Map<String, String>> buildChunks(List<GroundingChunk> chunks) {
        List<Map<String, String>> result = new ArrayList<>();
        if (CollUtil.isEmpty(chunks)) {
            return result;
        }
        StringBuilder total = new StringBuilder();
        for (GroundingChunk chunk : chunks) {
            if (chunk == null || StrUtil.isBlank(chunk.getText()) || total.length() >= MAX_CHUNKS_CHARS) {
                continue;
            }
            String text = StrUtil.subPre(chunk.getText(), MAX_CHUNKS_CHARS - total.length());
            Map<String, String> map = new LinkedHashMap<>();
            map.put("doc_name", StrUtil.nullToEmpty(chunk.getDocName()));
            map.put("text", text);
            result.add(map);
            total.append(text);
        }
        return result;
    }
}
