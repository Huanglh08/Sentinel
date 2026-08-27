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

import com.nageoffer.ai.ragent.rag.client.PythonAgentClient;
import com.nageoffer.ai.ragent.rag.config.MemoryProperties;
import com.nageoffer.ai.ragent.framework.trace.RagTraceNode;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

/**
 * 会话标题生成器
 * <p>
 * 按 g2 分层职责，会话标题改为调用 Python agent 的 {@code /generate-title} 接口，
 * 不再在 Java 侧直连 LLM。失败降级为"新对话"。
 * <p>
 * 拆为独立 bean 是为了让 Spring AOP 的 {@link RagTraceNode} 拦截生效。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class ConversationTitleGenerator {

    private final MemoryProperties memoryProperties;
    private final PythonAgentClient pythonAgentClient;

    @RagTraceNode(name = "conversation-title-gen", type = "TITLE_GEN")
    public String generate(String question) {
        int maxLen = memoryProperties.getTitleMaxLength();
        if (maxLen <= 0) {
            maxLen = 30;
        }
        try {
            String title = pythonAgentClient.generateTitle(question);
            if (title != null && !title.isEmpty()) {
                return title.length() > maxLen ? title.substring(0, maxLen) : title;
            }
        } catch (Exception ex) {
            log.warn("生成会话标题失败", ex);
        }
        return "新对话";
    }
}
