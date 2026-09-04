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

package com.nageoffer.ai.sentinel.rag.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.nageoffer.ai.sentinel.framework.convention.ChatMessage.MessageStatus;
import com.nageoffer.ai.sentinel.framework.convention.SourceRef;

import java.util.List;
import java.util.Map;

/**
 * 模型回复完成事件载荷
 *
 * @param messageId 消息ID（字符串，避免前端精度丢失）
 * @param title     会话标题（可选）
 * @param sources   文档级来源列表（可选，仅命中知识库时携带）
 * @param messageStatus 消息结束状态
 * @param ttftBreakdown TTFT 五段归因（可选，透传 Python agent 的 ttft_breakdown）
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record CompletionPayload(String messageId, String title, List<SourceRef> sources, MessageStatus messageStatus,
                                @JsonProperty("ttft_breakdown") Map<String, Object> ttftBreakdown) {

    /**
     * 无来源场景的便捷构造 sources 置空（NON_NULL 序列化时自动省略该字段）
     */
    public CompletionPayload(String messageId, String title) {
        this(messageId, title, null, MessageStatus.NORMAL, null);
    }

    /**
     * 兼容旧调用（限流拒绝等无归因数据场景）
     */
    public CompletionPayload(String messageId, String title, List<SourceRef> sources, MessageStatus messageStatus) {
        this(messageId, title, sources, messageStatus, null);
    }
}
