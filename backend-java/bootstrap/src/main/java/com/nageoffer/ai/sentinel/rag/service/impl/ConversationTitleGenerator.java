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

package com.nageoffer.ai.sentinel.rag.service.impl;

import com.nageoffer.ai.sentinel.rag.client.PythonAgentClient;
import com.nageoffer.ai.sentinel.framework.trace.RagTraceNode;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 会话标题生成器
 * <p>
 * 鎸?g2 鍒嗗眰鑱岃矗锛屼細璇濇爣棰樻敼涓鸿皟鐢?Python agent 鐨?{@code /generate-title} 鎺ュ彛锛?
 * 涓嶅啀鍦?Java 渚х洿杩?LLM銆傚け璐ラ檷绾т负"鏂板�硅�?銆?
 * <p>
 * 鎷嗕负鐙�绔� bean 鏄�涓轰簡璁� Spring AOP 鐨?{@link RagTraceNode} 鎷︽埅鐢熸晥銆?
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class ConversationTitleGenerator {

    private final PythonAgentClient pythonAgentClient;

    @Value("${rag.conversation.title-max-length:30}")
    private int titleMaxLength;

    @RagTraceNode(name = "conversation-title-gen", type = "TITLE_GEN")
    public String generate(String question) {
        int maxLen = titleMaxLength;
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
