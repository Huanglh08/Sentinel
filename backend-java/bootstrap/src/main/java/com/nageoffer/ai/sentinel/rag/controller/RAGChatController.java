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

package com.nageoffer.ai.sentinel.rag.controller;

import com.nageoffer.ai.sentinel.framework.convention.Result;
import com.nageoffer.ai.sentinel.framework.idempotent.IdempotentSubmit;
import com.nageoffer.ai.sentinel.framework.web.Results;
import com.nageoffer.ai.sentinel.rag.config.RAGDefaultProperties;
import com.nageoffer.ai.sentinel.rag.service.RAGChatService;
import com.nageoffer.ai.sentinel.rag.service.impl.PythonRAGChatServiceImpl;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * RAG 瀵硅瘽鎺у埗鍣?
 * 提供流式问答与任务取消接口
 */
@RestController
@RequiredArgsConstructor
public class RAGChatController {

    @Qualifier("pythonRAGChatServiceImpl")
    private final RAGChatService ragChatService;
    private final RAGDefaultProperties ragDefaultProperties;

    /**
     * 鍙戣捣 SSE 娴佸紡瀵硅瘽
     */
    @IdempotentSubmit(
            key = "T(com.nageoffer.ai.sentinel.framework.context.UserContext).getUserId()",
            message = "当前会话处理中，请稍后再发起新的对话"
    )
    @GetMapping(value = "/rag/v3/chat", produces = "text/event-stream;charset=UTF-8")
    public SseEmitter chat(@RequestParam String question,
                           @RequestParam(required = false) String conversationId,
                           @RequestParam(required = false, defaultValue = "false") Boolean deepThinking) {
        SseEmitter emitter = new SseEmitter(ragDefaultProperties.getSseTimeoutMs());
        ragChatService.streamChat(question, conversationId, deepThinking, emitter);
        return emitter;
    }

    /**
     * 停止指定任务
     */
    @IdempotentSubmit
    @PostMapping(value = "/rag/v3/stop")
    public Result<Void> stop(@RequestParam String taskId) {
        ragChatService.stopTask(taskId);
        return Results.success();
    }
}
