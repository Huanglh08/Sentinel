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

import cn.hutool.core.collection.CollUtil;
import cn.hutool.core.util.IdUtil;
import cn.hutool.core.util.StrUtil;
import com.nageoffer.ai.sentinel.framework.context.UserContext;
import com.nageoffer.ai.sentinel.framework.convention.ChatMessage;
import com.nageoffer.ai.sentinel.framework.convention.SourceRef;
import com.nageoffer.ai.sentinel.framework.web.SseEmitterSender;
import com.nageoffer.ai.sentinel.rag.client.PythonAgentClient;
import com.nageoffer.ai.sentinel.rag.core.retrieval.channel.KbCollectionProvider;
import com.nageoffer.ai.sentinel.rag.dao.entity.ConversationDO;
import com.nageoffer.ai.sentinel.rag.dto.CompletionPayload;
import com.nageoffer.ai.sentinel.rag.dto.MessageDelta;
import com.nageoffer.ai.sentinel.rag.dto.MetaPayload;
import com.nageoffer.ai.sentinel.rag.enums.ConversationMessageOrder;
import com.nageoffer.ai.sentinel.rag.enums.SSEEventType;
import com.nageoffer.ai.sentinel.rag.service.ConversationGroupService;
import com.nageoffer.ai.sentinel.rag.service.ConversationMessageService;
import com.nageoffer.ai.sentinel.rag.service.ConversationService;
import com.nageoffer.ai.sentinel.rag.service.RAGChatService;
import com.nageoffer.ai.sentinel.rag.service.bo.ConversationCreateBO;
import com.nageoffer.ai.sentinel.rag.service.bo.ConversationMessageBO;
import com.nageoffer.ai.sentinel.rag.controller.vo.ConversationMessageVO;
import com.nageoffer.ai.sentinel.rag.trace.StreamChatTraceRunner;
import com.nageoffer.ai.sentinel.rag.util.CitationMarkup;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;

/**
 * 基于 Python agent 的对话服务实现（g2 分层）。
 * <p>
 * 复刻 sentinel 的 {@code StreamChatEventHandler} 的 SSE 协议与落库行为：
 * <ol>
 *   <li>发送 META 帧（conversationId / taskId）</li>
 *   <li>透传 Python {@code /chat} 的 MESSAGE 帧（type=response, delta=token）</li>
 *   <li>收到 complete 后：落库 + FINISH（messageId / title / sources）→ DONE</li>
 * </ol>
 * 会话记忆由 Python agent 侧的「会话状态卡」承担；本类只负责消息持久化与
 * 最近 N 轮历史的透传（plumbing），不再包含 ragent 的滚动摘要机制。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class PythonRAGChatServiceImpl implements RAGChatService {

    private final PythonAgentClient pythonAgentClient;
    private final ConversationMessageService conversationMessageService;
    private final ConversationService conversationService;
    private final ConversationGroupService conversationGroupService;
    private final KbCollectionProvider kbCollectionProvider;
    private final StreamChatTraceRunner streamChatTraceRunner;

    @Value("${rag.conversation.history-keep-turns:8}")
    private int historyKeepTurns;

    @Override
    public void streamChat(String question, String conversationId, Boolean deepThinking, SseEmitter emitter) {
        // 1:1 ragent RAGChatServiceImpl#streamChat: generate snowflake id when blank,
        // so first-turn messages persist and frontend gets real id from META frame
        String actualConversationId = StrUtil.isBlank(conversationId)
                ? IdUtil.getSnowflakeNextIdStr()
                : conversationId;
        List<Map<String, String>> history = loadHistory(actualConversationId);
        List<String> knowledgeBaseIds = kbCollectionProvider.listActiveCollections();
        String userId = UserContext.getUserId();
        String taskId = UUID.randomUUID().toString();

        SseEmitterSender sender = new SseEmitterSender(emitter);

        // Persist user question first: creates conversation record (same as legacy StreamChatEventHandler).
        persistUserMessage(actualConversationId, userId, question);

        // 初始化：发送 META 帧
        sender.sendEvent(SSEEventType.META.value(), new MetaPayload(actualConversationId, taskId));

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
            // trace 采集：startRun + TTFT/TOOL/OUTCOME/USAGE 节点（dashboard AI 性能数据源）
            StreamChatTraceRunner.TraceHandlers trace = streamChatTraceRunner.begin(
                    question, actualConversationId, taskId, userId);
            try {
                pythonAgentClient.streamChat(
                        question,
                        actualConversationId,
                        taskId,
                        knowledgeBaseIds,
                        history,
                        trace.onToken(delta -> {
                            // 透传 MESSAGE 帧（type=response）
                            answer.append(delta);
                            sender.sendEvent(SSEEventType.MESSAGE.value(), new MessageDelta("response", delta));
                        }),
                        trace.onThink(thinkDelta -> {
                            // 透传 THINK 帧（thinking 内容）
                            sender.sendEvent(SSEEventType.THINK.value(), new MessageDelta("think", thinkDelta));
                        }),
                        trace.onCompleteData(completeData -> {
                            // 流结束：落库 + FINISH 帧 + DONE
                            String fullText = (String) completeData.getOrDefault("full_text", answer.toString());
                            String messageId = persistMessage(actualConversationId, userId, fullText);
                            String title = resolveTitle(actualConversationId, userId);
                            // sources 用于前端引用面板
                            List<SourceRef> sources = buildSourceRefs(completeData);
                            // ttft_breakdown 五段归因透传（评测 /debug 用，前端忽略未知字段）
                            Map<String, Object> ttftBreakdown =
                                    (Map<String, Object>) completeData.get("ttft_breakdown");
                            sender.sendEvent(SSEEventType.FINISH.value(),
                                    new CompletionPayload(messageId, title, sources,
                                            ChatMessage.MessageStatus.NORMAL, ttftBreakdown));
                            sender.sendEvent(SSEEventType.DONE.value(), "[DONE]");
                            sender.complete();
                        }),
                        trace.onError(error -> {
                            log.error("Python /chat stream error, conversationId={}", actualConversationId, error);
                            sender.fail(error);
                        })
                );
            } catch (Exception e) {
                trace.failSync(e);
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

    /**
     * 消息持久化（原 ConversationMemoryStore.append 的落库部分内联）：
     * 写消息 + 首条用户消息时创建/更新会话记录（标题由 ConversationService 内部生成）。
     */
    private String appendMessage(String conversationId, String userId, ChatMessage message) {
        ConversationMessageBO conversationMessage = ConversationMessageBO.builder()
                .conversationId(conversationId)
                .userId(userId)
                .role(message.getRole().name().toLowerCase())
                .content(message.getContent())
                .thinkingContent(message.getThinkingContent())
                .thinkingDuration(message.getThinkingDuration())
                .sources(message.getSources())
                .retrievedChunks(message.getRetrievedChunks())
                .replyToMessageId(message.getReplyToMessageId())
                .messageStatus(message.getMessageStatus() == null ? null : message.getMessageStatus().name())
                .build();
        String messageId = conversationMessageService.addMessage(conversationMessage);
        if (message.getRole() == ChatMessage.Role.USER) {
            ConversationCreateBO conversation = ConversationCreateBO.builder()
                    .conversationId(conversationId)
                    .userId(userId)
                    .question(message.getContent())
                    .lastTime(new Date())
                    .build();
            conversationService.createOrUpdate(conversation);
        }
        return messageId;
    }

    private String persistMessage(String conversationId, String userId, String content) {
        if (StrUtil.isBlank(content)) {
            return null;
        }
        try {
            ChatMessage message = ChatMessage.assistant(content);
            message.setMessageStatus(ChatMessage.MessageStatus.NORMAL);
            return appendMessage(conversationId, userId, message);
        } catch (Exception e) {
            log.error("对话完成时持久化消息失败，conversationId：{}", conversationId, e);
            return null;
        }
    }

    private void persistUserMessage(String conversationId, String userId, String question) {
        if (StrUtil.isBlank(question)) {
            return;
        }
        try {
            appendMessage(conversationId, userId, ChatMessage.user(question));
        } catch (Exception e) {
            log.error("persist user message failed, conversationId={}", conversationId, e);
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
        return "\u65b0\u5bf9\u8bdd";
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

    /**
     * 读取最近 history-keep-turns 轮对话（plumbing 透传，不做摘要）。
     * 长期记忆由 Python agent 的会话状态卡承担，超出窗口的内容经状态卡延续。
     */
    private List<Map<String, String>> loadHistory(String conversationId) {
        List<Map<String, String>> history = new ArrayList<>();
        if (conversationId == null || conversationId.isBlank()) {
            return history;
        }
        int limit = historyKeepTurns > 0 ? historyKeepTurns * 2 : 16;
        String userId = UserContext.getUserId();
        try {
            List<ConversationMessageVO> messages = conversationMessageService.listMessages(
                    conversationId, userId, limit, ConversationMessageOrder.DESC);
            if (CollUtil.isEmpty(messages)) {
                return history;
            }
            List<ChatMessage> cleaned = new ArrayList<>();
            for (int i = messages.size() - 1; i >= 0; i--) {
                ChatMessage m = toChatMessage(messages.get(i));
                if (m != null) {
                    cleaned.add(m);
                }
            }
            // 裁掉首条孤立 assistant（旧会话尾部回复）：保持 user 起始的成对结构
            int start = 0;
            while (start < cleaned.size() && cleaned.get(start).getRole() == ChatMessage.Role.ASSISTANT) {
                start++;
            }
            for (ChatMessage m : cleaned.subList(start, cleaned.size())) {
                Map<String, String> map = new LinkedHashMap<>();
                map.put("role", m.getRole().name().toLowerCase());
                map.put("content", m.getContent());
                history.add(map);
            }
        } catch (Exception e) {
            log.warn("读取会话历史失败 conversationId={}", conversationId, e);
        }
        return history;
    }

    private ChatMessage toChatMessage(ConversationMessageVO record) {
        if (record == null || StrUtil.isBlank(record.getContent())) {
            return null;
        }
        ChatMessage.Role role = ChatMessage.Role.fromString(record.getRole());
        String content = role == ChatMessage.Role.ASSISTANT
                ? CitationMarkup.strip(record.getContent())
                : record.getContent();
        return new ChatMessage(role, content);
    }
}
