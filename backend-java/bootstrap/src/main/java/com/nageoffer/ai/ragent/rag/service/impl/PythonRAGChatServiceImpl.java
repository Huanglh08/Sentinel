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

import cn.hutool.core.util.StrUtil;
import com.nageoffer.ai.ragent.framework.context.UserContext;
import com.nageoffer.ai.ragent.framework.convention.ChatMessage;
import com.nageoffer.ai.ragent.framework.convention.SourceRef;
import com.nageoffer.ai.ragent.framework.web.SseEmitterSender;
import com.nageoffer.ai.ragent.rag.client.PythonAgentClient;
import com.nageoffer.ai.ragent.rag.config.MemoryProperties;
import com.nageoffer.ai.ragent.rag.controller.vo.ConversationMessageVO;
import com.nageoffer.ai.ragent.rag.core.memory.ConversationMemoryService;
import com.nageoffer.ai.ragent.rag.core.retrieval.channel.KbCollectionProvider;
import com.nageoffer.ai.ragent.rag.dao.entity.ConversationDO;
import com.nageoffer.ai.ragent.rag.dto.CompletionPayload;
import com.nageoffer.ai.ragent.rag.dto.MessageDelta;
import com.nageoffer.ai.ragent.rag.dto.MetaPayload;
import com.nageoffer.ai.ragent.rag.enums.ConversationMessageOrder;
import com.nageoffer.ai.ragent.rag.enums.SSEEventType;
import com.nageoffer.ai.ragent.rag.service.ConversationGroupService;
import com.nageoffer.ai.ragent.rag.service.ConversationMessageService;
import com.nageoffer.ai.ragent.rag.service.RAGChatService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;

/**
 * 基于 Python agent 的对话服务实现（g2 分层）。
 * <p>
 * 复刻 ragent 原 {@code StreamChatEventHandler} 的 SSE 协议与落库行为：
 * <ol>
 *   <li>发送 META 帧（conversationId / taskId）</li>
 *   <li>透传 Python {@code /chat} 的 MESSAGE 帧（type=response, delta=token）</li>
 *   <li>收到 complete 后：落库 → 发 FINISH（messageId / title / sources）→ 发 DONE</li>
 * </ol>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class PythonRAGChatServiceImpl implements RAGChatService {

    private final PythonAgentClient pythonAgentClient;
    private final ConversationMessageService conversationMessageService;
    private final ConversationMemoryService conversationMemoryService;
    private final ConversationGroupService conversationGroupService;
    private final MemoryProperties memoryProperties;
    private final KbCollectionProvider kbCollectionProvider;

    @Override
    public void streamChat(String question, String conversationId, Boolean deepThinking, SseEmitter emitter) {
        List<Map<String, String>> history = loadHistory(conversationId);
        List<String> knowledgeBaseIds = kbCollectionProvider.listActiveCollections();
        String userId = UserContext.getUserId();
        String taskId = UUID.randomUUID().toString();

        SseEmitterSender sender = new SseEmitterSender(emitter);

        // 初始化：发送 META 帧
        sender.sendEvent(SSEEventType.META.value(), new MetaPayload(conversationId, taskId));

        emitter.onTimeout(() -> {
            try {
                sender.complete();
            } catch (Exception ignored) {
            }
        });
        emitter.onError(e -> log.warn("SseEmitter 异常 conversationId={}", conversationId, e));

        // 在独立线程执行阻塞式 SSE 转发
        CompletableFuture.runAsync(() -> {
            StringBuilder answer = new StringBuilder();
            try {
                pythonAgentClient.streamChat(
                        question,
                        conversationId,
                        taskId,
                        knowledgeBaseIds,
                        history,
                        delta -> {
                            // 透传 MESSAGE 帧（type=response）
                            answer.append(delta);
                            sender.sendEvent(SSEEventType.MESSAGE.value(), new MessageDelta("response", delta));
                        },
                        thinkDelta -> {
                            // 透传 THINK 帧（thinking 内容）
                            sender.sendEvent(SSEEventType.THINK.value(), new MessageDelta("think", thinkDelta));
                        },
                        completeData -> {
                            // 流结束：落库 → FINISH → DONE
                            String fullText = (String) completeData.getOrDefault("full_text", answer.toString());
                            String messageId = persistMessage(conversationId, userId, fullText);
                            String title = resolveTitle(conversationId, userId);
                            // sources 用于前端引用面板
                            List<SourceRef> sources = buildSourceRefs(completeData);
                            sender.sendEvent(SSEEventType.FINISH.value(),
                                    new CompletionPayload(messageId, title, sources, ChatMessage.MessageStatus.NORMAL));
                            sender.sendEvent(SSEEventType.DONE.value(), "[DONE]");
                            sender.complete();
                        },
                        error -> {
                            log.error("Python /chat 流异常 conversationId={}", conversationId, error);
                            sender.fail(error);
                        }
                );
            } catch (Exception e) {
                log.error("转发 Python /chat 失败 conversationId={}", conversationId, e);
                sender.fail(e);
            }
        });
    }

    @Override
    public void stopTask(String taskId) {
        try {
            pythonAgentClient.stopTask(taskId);
        } catch (Exception e) {
            log.warn("调用 Python /stop 失败 taskId={}", taskId, e);
        }
    }

    private String persistMessage(String conversationId, String userId, String content) {
        if (StrUtil.isBlank(content)) {
            return null;
        }
        try {
            ChatMessage message = ChatMessage.assistant(content);
            message.setMessageStatus(ChatMessage.MessageStatus.NORMAL);
            return conversationMemoryService.append(conversationId, userId, message);
        } catch (Exception e) {
            log.error("对话完成时持久化消息失败，conversationId：{}", conversationId, e);
            return null;
        }
    }

    private String resolveTitle(String conversationId, String userId) {
        try {
            ConversationDO existing = conversationGroupService.findConversation(conversationId, userId);
            if (existing != null && StrUtil.isNotBlank(existing.getTitle())) {
                return existing.getTitle();
            }
        } catch (Exception e) {
            log.warn("查询会话标题失败 conversationId={}", conversationId, e);
        }
        return "新对话";
    }

    private List<SourceRef> buildSourceRefs(Map<String, Object> completeData) {
        List<Map<String, Object>> raw = (List<Map<String, Object>>) completeData.get("sources");
        if (raw == null || raw.isEmpty()) {
            return List.of();
        }
        List<SourceRef> result = new ArrayList<>();
        for (Map<String, Object> item : raw) {
            if (item == null) {
                continue;
            }
            SourceRef ref = new SourceRef();
            Object idx = item.get("index");
            ref.setIndex(idx instanceof Number ? ((Number) idx).intValue() : null);
            ref.setDocId(StrUtil.nullToEmpty((String) item.get("doc_id")));
            ref.setDocName(StrUtil.nullToEmpty((String) item.get("doc_name")));
            ref.setSourceType(StrUtil.nullToEmpty((String) item.get("source_type")));
            ref.setFileType(StrUtil.nullToEmpty((String) item.get("file_type")));
            ref.setUrl(StrUtil.nullToEmpty((String) item.get("url")));
            ref.setExcerpt(StrUtil.nullToEmpty((String) item.get("excerpt")));
            result.add(ref);
        }
        return result;
    }

    private List<Map<String, String>> loadHistory(String conversationId) {
        List<Map<String, String>> history = new ArrayList<>();
        if (conversationId == null || conversationId.isBlank()) {
            return history;
        }
        int limit = memoryProperties.getHistoryKeepTurns() * 2;
        if (limit <= 0) {
            limit = 20;
        }
        String userId = UserContext.getUserId();
        try {
            List<ConversationMessageVO> messages = conversationMessageService.listMessages(
                    conversationId, userId, limit, ConversationMessageOrder.DESC);
            for (int i = messages.size() - 1; i >= 0; i--) {
                ConversationMessageVO m = messages.get(i);
                if (m == null || m.getRole() == null || m.getContent() == null) {
                    continue;
                }
                String role = m.getRole().toLowerCase();
                if (!"user".equals(role) && !"assistant".equals(role)) {
                    continue;
                }
                Map<String, String> map = new LinkedHashMap<>();
                map.put("role", role);
                map.put("content", m.getContent());
                history.add(map);
            }
        } catch (Exception e) {
            log.warn("读取会话历史失败 conversationId={}", conversationId, e);
        }
        return history;
    }
}
