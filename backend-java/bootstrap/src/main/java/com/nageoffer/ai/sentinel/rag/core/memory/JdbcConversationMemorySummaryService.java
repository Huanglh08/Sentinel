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

package com.nageoffer.ai.sentinel.rag.core.memory;

import cn.hutool.core.collection.CollUtil;
import cn.hutool.core.util.StrUtil;
import com.nageoffer.ai.sentinel.framework.convention.ChatMessage;
import com.nageoffer.ai.sentinel.rag.util.CitationMarkup;
import com.nageoffer.ai.sentinel.rag.client.PythonAgentClient;
import com.nageoffer.ai.sentinel.rag.config.MemoryProperties;
import com.nageoffer.ai.sentinel.rag.dao.entity.ConversationMessageDO;
import com.nageoffer.ai.sentinel.rag.dao.entity.ConversationSummaryDO;
import com.nageoffer.ai.sentinel.rag.service.ConversationGroupService;
import com.nageoffer.ai.sentinel.rag.service.ConversationMessageService;
import com.nageoffer.ai.sentinel.rag.service.bo.ConversationSummaryBO;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;

@Slf4j
@Service
@RequiredArgsConstructor
public class JdbcConversationMemorySummaryService implements ConversationMemorySummaryService {

    private static final String SUMMARY_LOCK_PREFIX = "sentinel:memory:summary:lock:";

    private final ConversationGroupService conversationGroupService;
    private final ConversationMessageService conversationMessageService;
    private final MemoryProperties memoryProperties;
    private final PythonAgentClient pythonAgentClient;
    private final RedissonClient redissonClient;
    private final Executor memorySummaryExecutor;

    @Override
    public void compressIfNeeded(String conversationId, String userId, ChatMessage message) {
        if (!memoryProperties.getSummaryEnabled()) {
            return;
        }
        if (message.getRole() != ChatMessage.Role.ASSISTANT) {
            return;
        }
        CompletableFuture.runAsync(() -> doCompressIfNeeded(conversationId, userId), memorySummaryExecutor)
                .exceptionally(ex -> {
                    log.error("对话记忆摘要异步任务失败 - conversationId: {}, userId: {}",
                            conversationId, userId, ex);
                    return null;
                });
    }

    @Override
    public ChatMessage loadLatestSummary(String conversationId, String userId) {
        ConversationSummaryDO summary = conversationGroupService.findLatestSummary(conversationId, userId);
        return toChatMessage(summary);
    }

    @Override
    public ChatMessage decorateIfNeeded(ChatMessage summary) {
        if (summary == null || StrUtil.isBlank(summary.getContent())) {
            return summary;
        }
        return ChatMessage.system(summary.getContent().trim());
    }

    private void doCompressIfNeeded(String conversationId, String userId) {
        long startTime = System.currentTimeMillis();
        int triggerTurns = memoryProperties.getSummaryStartTurns();
        int maxTurns = memoryProperties.getHistoryKeepTurns();
        if (maxTurns <= 0 || triggerTurns <= 0) {
            return;
        }

        String lockKey = SUMMARY_LOCK_PREFIX + buildLockKey(conversationId, userId);
        RLock lock = redissonClient.getLock(lockKey);
        if (!lock.tryLock()) {
            return;
        }
        try {
            long total = conversationGroupService.countUserMessages(conversationId, userId);
            if (total < triggerTurns) {
                return;
            }

            ConversationSummaryDO latestSummary = conversationGroupService.findLatestSummary(conversationId, userId);
            List<ConversationMessageDO> latestUserTurns = conversationGroupService.listLatestUserOnlyMessages(
                    conversationId,
                    userId,
                    maxTurns
            );
            if (latestUserTurns.isEmpty()) {
                return;
            }
            String historyStartId = resolveHistoryStartId(latestUserTurns);
            if (StrUtil.isBlank(historyStartId)) {
                return;
            }

            String afterId = resolveSummaryStartId(conversationId, userId, latestSummary);
            if (afterId != null && Long.parseLong(afterId) >= Long.parseLong(historyStartId)) {
                return;
            }

            // 摘要覆盖约一半原文窗口；只有这段重叠滑出窗口后才再次生成摘要
            String summaryCutoffId = resolveSummaryCutoffId(latestUserTurns);
            if (StrUtil.isBlank(summaryCutoffId)) {
                return;
            }

            List<ConversationMessageDO> toSummarize = conversationGroupService.listMessagesBetweenIds(
                    conversationId,
                    userId,
                    afterId,
                    summaryCutoffId
            );
            if (CollUtil.isEmpty(toSummarize)) {
                return;
            }

            String lastMessageId = resolveLastMessageId(toSummarize);
            if (StrUtil.isBlank(lastMessageId)) {
                return;
            }

            String existingSummary = latestSummary == null ? "" : latestSummary.getContent();
            String summary = summarizeMessages(toSummarize, existingSummary);
            if (StrUtil.isBlank(summary)) {
                return;
            }

            createSummary(conversationId, userId, summary, lastMessageId);
            log.info("摘要成功 - conversationId：{}，userId：{}，消息数：{}，耗时：{}ms",
                    conversationId, userId, toSummarize.size(),
                    System.currentTimeMillis() - startTime);
        } catch (Exception e) {
            log.error("摘要失败 - conversationId：{}，userId：{}", conversationId, userId, e);
        } finally {
            if (lock.isHeldByCurrentThread()) {
                lock.unlock();
            }
        }
    }

    private String summarizeMessages(List<ConversationMessageDO> messages, String existingSummary) {
        List<Map<String, String>> history = toHistoryMaps(messages);
        if (CollUtil.isEmpty(history)) {
            return existingSummary;
        }
        int summaryMaxChars = memoryProperties.getSummaryMaxChars();
        try {
            String result = pythonAgentClient.summarize(history, existingSummary);
            if (StrUtil.isBlank(result)) {
                return existingSummary;
            }
            if (result.length() > summaryMaxChars) {
                result = result.substring(0, summaryMaxChars);
            }
            log.info("瀵硅瘽鎽樿�佺敓鎴� - resultChars: {}", result.length());
            return result;
        } catch (Exception e) {
            log.error("对话记忆摘要生成失败, conversationId相关消息数: {}", messages.size(), e);
            return existingSummary;
        }
    }

    private List<Map<String, String>> toHistoryMaps(List<ConversationMessageDO> messages) {
        if (CollUtil.isEmpty(messages)) {
            return List.of();
        }
        List<Map<String, String>> result = new ArrayList<>();
        for (ConversationMessageDO item : messages) {
            if (item == null || StrUtil.isBlank(item.getContent()) || StrUtil.isBlank(item.getRole())) {
                continue;
            }
            String role = item.getRole().toLowerCase();
            if (!"user".equals(role) && !"assistant".equals(role)) {
                continue;
            }
            String content = "assistant".equals(role) ? CitationMarkup.strip(item.getContent()) : item.getContent();
            Map<String, String> map = new LinkedHashMap<>();
            map.put("role", role);
            map.put("content", content);
            result.add(map);
        }
        return result;
    }

    private ChatMessage toChatMessage(ConversationSummaryDO record) {
        if (record == null || StrUtil.isBlank(record.getContent())) {
            return null;
        }
        return new ChatMessage(ChatMessage.Role.SYSTEM, record.getContent());
    }

    private String resolveSummaryStartId(String conversationId, String userId, ConversationSummaryDO summary) {
        if (summary == null) {
            return null;
        }
        if (summary.getLastMessageId() != null) {
            return summary.getLastMessageId();
        }

        Date after = summary.getUpdateTime();
        if (after == null) {
            after = summary.getCreateTime();
        }
        return conversationGroupService.findMaxMessageIdAtOrBefore(conversationId, userId, after);
    }

    private String resolveHistoryStartId(List<ConversationMessageDO> latestUserTurns) {
        if (CollUtil.isEmpty(latestUserTurns)) {
            return null;
        }

        // 倒序列表的最后一个就是最早的
        ConversationMessageDO oldest = latestUserTurns.get(latestUserTurns.size() - 1);
        return oldest == null ? null : oldest.getId();
    }

    private String resolveSummaryCutoffId(List<ConversationMessageDO> latestUserTurns) {
        if (CollUtil.isEmpty(latestUserTurns)) {
            return null;
        }

        ConversationMessageDO overlapBoundary = latestUserTurns.get((latestUserTurns.size() - 1) / 2);
        return overlapBoundary == null ? null : overlapBoundary.getId();
    }

    private String resolveLastMessageId(List<ConversationMessageDO> toSummarize) {
        for (int i = toSummarize.size() - 1; i >= 0; i--) {
            ConversationMessageDO item = toSummarize.get(i);
            if (item != null && item.getId() != null) {
                return item.getId();
            }
        }
        return null;
    }

    private void createSummary(String conversationId,
                               String userId,
                               String content,
                               String lastMessageId) {
        ConversationSummaryBO summaryRecord = ConversationSummaryBO.builder()
                .conversationId(conversationId)
                .userId(userId)
                .content(content)
                .lastMessageId(lastMessageId)
                .build();
        conversationMessageService.addMessageSummary(summaryRecord);
    }

    private String buildLockKey(String conversationId, String userId) {
        return userId.trim() + ":" + conversationId.trim();
    }
}
