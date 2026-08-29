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

package com.nageoffer.ai.sentinel.rag.trace;

import cn.hutool.core.util.IdUtil;
import cn.hutool.core.util.StrUtil;
import cn.hutool.json.JSONUtil;
import com.nageoffer.ai.sentinel.rag.config.RagTraceProperties;
import com.nageoffer.ai.sentinel.rag.dao.entity.RagTraceNodeDO;
import com.nageoffer.ai.sentinel.rag.dao.entity.RagTraceRunDO;
import com.nageoffer.ai.sentinel.rag.service.RagTraceRecordService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 流式对话 Trace 采集器（移植自 ragent StreamChatTraceRunner，适配 sentinel 函数式回调链路）。
 * <p>
 * 职责与 ragent 版一致：
 * <ol>
 *   <li>startRun / finishRun：一次对话 = 一条 run 记录（dashboard AI 性能面板的数据源）</li>
 *   <li>TTFT 节点（user-first-packet）：用户感知首包延迟</li>
 *   <li>TOOL 节点（python-agent-chat）：工具调用成功率数据源</li>
 *   <li>OUTCOME 节点（answer-outcome）：自主完成 / 拒答 / 空检索 分布 + 风险类别分布</li>
 *   <li>USAGE 节点（generation-usage）：token 估算与生成速度 TPS</li>
 * </ol>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class StreamChatTraceRunner {

    public static final String STATUS_SUCCESS = "SUCCESS";
    public static final String STATUS_ERROR = "ERROR";

    /** 节点类型：用户感知首包（TTFT） */
    public static final String NODE_TYPE_TTFT = "TTFT";
    /** 节点类型：工具/外部服务调用 */
    public static final String NODE_TYPE_TOOL = "TOOL";
    /** 节点类型：回答结果（autonomous / refusal / no_doc / no_retrieval + 风险类别） */
    public static final String NODE_TYPE_OUTCOME = "OUTCOME";
    /** 节点类型：生成用量（token 估算 / TPS） */
    public static final String NODE_TYPE_USAGE = "USAGE";

    /** 回答结果：自主完成（有引用、正常回答） */
    public static final String OUTCOME_AUTONOMOUS = "autonomous";
    /** 回答结果：拒答 */
    public static final String OUTCOME_REFUSAL = "refusal";
    /** 回答结果：无文档兜底话术 */
    public static final String OUTCOME_NO_DOC = "no_doc";
    /** 回答结果：空检索（无 sources 且非兜底话术） */
    public static final String OUTCOME_NO_RETRIEVAL = "no_retrieval";

    private static final Pattern RISK_CATEGORY_PATTERN = Pattern.compile("LLM\\d{2}");
    /** 无结果兜底话术特征（与 Python 侧 NO_DOC_REPLY 对齐） */
    private static final String NO_DOC_MARK = "未检索到";
    /** 拒答话术特征（安全垂类拒答监控） */
    private static final String[] REFUSAL_MARKS = {"无法回答", "无法提供", "我不能", "拒绝回答", "抱歉，我无法", "当前可用信息不足"};

    private final RagTraceRecordService ragTraceRecordService;
    private final RagTraceProperties ragTraceProperties;

    /**
     * 开启一次流式对话的 trace 采集，返回回调包装器。
     * 调用方将原 onToken/onThink/onCompleteData/onError 回调经包装器包装后再传给客户端。
     */
    public TraceHandlers begin(String question, String conversationId, String taskId, String userId) {
        long startMs = System.currentTimeMillis();
        TraceHandlers handlers = new TraceHandlers(startMs);
        if (!ragTraceProperties.isEnabled()) {
            handlers.enabled = false;
            return handlers;
        }
        try {
            Map<String, Object> extra = new LinkedHashMap<>();
            extra.put("question", StrUtil.nullToEmpty(question));
            extra.put("questionLength", StrUtil.length(question));
            RagTraceRunDO run = RagTraceRunDO.builder()
                    .traceId(handlers.traceId)
                    .traceName("python-agent-chat")
                    .entryMethod("PythonRAGChatServiceImpl#streamChat")
                    .conversationId(conversationId)
                    .taskId(taskId)
                    .userId(userId)
                    .status("RUNNING")
                    .startTime(new Date(startMs))
                    .extraData(JSONUtil.toJsonStr(extra))
                    .build();
            ragTraceRecordService.startRun(run);
        } catch (Exception e) {
            log.warn("[rag-trace] startRun failed, traceId={}", handlers.traceId, e);
            handlers.enabled = false;
        }
        return handlers;
    }

    /**
     * 流式对话 trace 回调包装器。所有包装方法保证：trace 失败不影响业务链路。
     */
    public class TraceHandlers {

        private final String traceId = IdUtil.fastSimpleUUID();
        private final long startMs;
        private final AtomicBoolean ttftRecorded = new AtomicBoolean(false);
        private final AtomicBoolean finished = new AtomicBoolean(false);
        private volatile boolean enabled = true;
        /** 首个正文 delta 时间戳（CAS 写入,仅记录一次,用于 TTFT 节点） */
        private final AtomicLong firstTokenAt = new AtomicLong(0L);
        /** 首个输出 delta（thinking 或正文）时间戳：TPS 生成窗口起点 */
        private final AtomicLong firstOutputAt = new AtomicLong(0L);
        /** thinking 累计字符数（推理模型输出主体为中文,近似按 1 token/字 估算） */
        private final AtomicLong thinkChars = new AtomicLong(0L);

        private TraceHandlers(long startMs) {
            this.startMs = startMs;
        }

        public String getTraceId() {
            return traceId;
        }

        /** 包装增量回调：首个内容 delta 记录用户感知首包（TTFT） */
        public Consumer<String> onToken(Consumer<String> delegate) {
            return delta -> {
                recordTtft();
                if (delegate != null) {
                    delegate.accept(delta);
                }
            };
        }

        /** 包装思考过程回调：不计入 TTFT，但累计 thinking 字符并参与 TPS 生成窗口起点 */
        public Consumer<String> onThink(Consumer<String> delegate) {
            return delta -> {
                if (enabled) {
                    long now = System.currentTimeMillis();
                    firstOutputAt.compareAndSet(0L, now);
                    thinkChars.addAndGet(StrUtil.length(delta));
                }
                if (delegate != null) {
                    delegate.accept(delta);
                }
            };
        }

        /** 包装完成回调：记录 TOOL/OUTCOME/USAGE 节点 + finishRun(SUCCESS) */
        @SuppressWarnings("unchecked")
        public Consumer<Map<String, Object>> onCompleteData(Consumer<Map<String, Object>> delegate) {
            return data -> {
                try {
                    recordSuccess(data);
                } catch (Exception e) {
                    log.warn("[rag-trace] record success failed, traceId={}", traceId, e);
                }
                if (delegate != null) {
                    delegate.accept(data);
                }
            };
        }

        /** 包装错误回调：记录 TOOL 节点 ERROR + finishRun(ERROR) */
        public Consumer<Throwable> onError(Consumer<Throwable> delegate) {
            return t -> {
                try {
                    recordError(t == null ? null : t.getMessage());
                } catch (Exception e) {
                    log.warn("[rag-trace] record error failed, traceId={}", traceId, e);
                }
                if (delegate != null) {
                    delegate.accept(t);
                }
            };
        }

        /** 同步阶段异常（streamChat 调用本身抛出、未走到回调）时手动调用 */
        public void failSync(Throwable t) {
            try {
                recordError(t == null ? null : t.getMessage());
            } catch (Exception e) {
                log.warn("[rag-trace] record sync error failed, traceId={}", traceId, e);
            }
        }

        // ---------- 内部实现 ----------

        private void recordTtft() {
            if (!enabled) {
                return;
            }
            long now = System.currentTimeMillis();
            firstTokenAt.compareAndSet(0L, now);
            firstOutputAt.compareAndSet(0L, now);
            if (!ttftRecorded.compareAndSet(false, true)) {
                return;
            }
            try {
                RagTraceNodeDO node = RagTraceNodeDO.builder()
                        .traceId(traceId)
                        .nodeId(IdUtil.fastSimpleUUID())
                        .nodeType(NODE_TYPE_TTFT)
                        .nodeName("user-first-packet")
                        .depth(1)
                        .status(STATUS_SUCCESS)
                        .startTime(new Date(startMs))
                        .endTime(new Date(now))
                        .durationMs(now - startMs)
                        .build();
                ragTraceRecordService.startNode(node);
            } catch (Exception e) {
                log.warn("[rag-trace] ttft node failed, traceId={}", traceId, e);
            }
        }

        @SuppressWarnings("unchecked")
        private void recordSuccess(Map<String, Object> data) {
            if (!enabled || !finished.compareAndSet(false, true)) {
                return;
            }
            long now = System.currentTimeMillis();
            // 1. TOOL 节点：python-agent-chat 调用成功
            insertNode(NODE_TYPE_TOOL, "python-agent-chat", STATUS_SUCCESS, null, now, now - startMs);
            // 2. OUTCOME 节点：回答结果 + 风险类别
            String fullText = data == null ? null : StrUtil.toStringOrNull(data.get("full_text"));
            List<Map<String, Object>> sources = data == null ? null : (List<Map<String, Object>>) data.get("sources");
            recordOutcome(fullText, sources, now);
            // 3. USAGE 节点：token 估算 + TPS
            recordUsage(fullText, now);
            // 4. finishRun
            finishRun(STATUS_SUCCESS, null, now);
        }

        private void recordError(String message) {
            if (!enabled || !finished.compareAndSet(false, true)) {
                return;
            }
            long now = System.currentTimeMillis();
            insertNode(NODE_TYPE_TOOL, "python-agent-chat", STATUS_ERROR,
                    truncate(message), now, now - startMs);
            finishRun(STATUS_ERROR, truncate(message), now);
        }

        private void recordOutcome(String fullText, List<Map<String, Object>> sources, long now) {
            String text = StrUtil.nullToEmpty(fullText);
            boolean emptySources = sources == null || sources.isEmpty();
            String outcome;
            if (emptySources) {
                if (text.contains(NO_DOC_MARK)) {
                    outcome = OUTCOME_NO_DOC;
                } else if (containsAny(text, REFUSAL_MARKS)) {
                    outcome = OUTCOME_REFUSAL;
                } else {
                    outcome = OUTCOME_NO_RETRIEVAL;
                }
            } else if (containsAny(text, REFUSAL_MARKS)) {
                // 带引用但明确拒答：安全垂类常见"检索命中文档但模型拒绝回答"，
                // 必须全量检查文本，否则会被误判为 autonomous 导致拒答率恒 0
                outcome = OUTCOME_REFUSAL;
            } else {
                outcome = OUTCOME_AUTONOMOUS;
            }
            // 风险类别分布：从 sources 的 doc_name 提取 LLM01~LLM10
            Set<String> riskCategories = new LinkedHashSet<>();
            if (!emptySources) {
                for (Map<String, Object> source : sources) {
                    if (source == null) {
                        continue;
                    }
                    Object docName = source.get("doc_name");
                    if (docName != null) {
                        Matcher matcher = RISK_CATEGORY_PATTERN.matcher(docName.toString());
                        while (matcher.find()) {
                            riskCategories.add(matcher.group());
                        }
                    }
                }
            }
            Map<String, Object> extra = new LinkedHashMap<>();
            extra.put("outcome", outcome);
            extra.put("riskCategories", riskCategories);
            extra.put("sourceCount", emptySources ? 0 : sources.size());
            insertNode(NODE_TYPE_OUTCOME, "answer-outcome", STATUS_SUCCESS, null, now, 0L, JSONUtil.toJsonStr(extra));
        }

        private void recordUsage(String fullText, long now) {
            long textTokens = estimateTokens(fullText);
            // thinking 以中文为主，近似按 1 token/字 计入输出 token（此前遗漏导致 TPS 严重偏高）
            long thinkTokenEstimate = thinkChars.get();
            long outputTokens = textTokens + thinkTokenEstimate;
            long firstAt = firstOutputAt.get();
            long generationMs = firstAt > 0 ? now - firstAt : 0L;
            double tps = generationMs > 0 ? (outputTokens * 1000.0) / generationMs : 0.0;
            Map<String, Object> extra = new LinkedHashMap<>();
            extra.put("estOutputTokens", outputTokens);
            extra.put("textTokens", textTokens);
            extra.put("thinkTokens", thinkTokenEstimate);
            extra.put("generationMs", generationMs);
            extra.put("tps", Math.round(tps * 10.0) / 10.0);
            extra.put("estimated", true);
            insertNode(NODE_TYPE_USAGE, "generation-usage", STATUS_SUCCESS, null, now, generationMs, JSONUtil.toJsonStr(extra));
        }

        private void insertNode(String nodeType, String nodeName, String status,
                                String errorMessage, long endTime, long durationMs) {
            insertNode(nodeType, nodeName, status, errorMessage, endTime, durationMs, null);
        }

        private void insertNode(String nodeType, String nodeName, String status,
                                String errorMessage, long endTime, long durationMs, String extraData) {
            try {
                RagTraceNodeDO node = RagTraceNodeDO.builder()
                        .traceId(traceId)
                        .nodeId(IdUtil.fastSimpleUUID())
                        .nodeType(nodeType)
                        .nodeName(nodeName)
                        .depth(1)
                        .status(status)
                        .errorMessage(errorMessage)
                        .startTime(new Date(startMs))
                        .endTime(new Date(endTime))
                        .durationMs(durationMs)
                        .extraData(extraData)
                        .build();
                ragTraceRecordService.startNode(node);
            } catch (Exception e) {
                log.warn("[rag-trace] {} node failed, traceId={}", nodeType, traceId, e);
            }
        }

        private void finishRun(String status, String errorMessage, long endTime) {
            try {
                ragTraceRecordService.finishRun(traceId, status, errorMessage, new Date(endTime), endTime - startMs);
            } catch (Exception e) {
                log.warn("[rag-trace] finishRun failed, traceId={}", traceId, e);
            }
        }

        private boolean containsAny(String text, String[] marks) {
            for (String mark : marks) {
                if (text.contains(mark)) {
                    return true;
                }
            }
            return false;
        }

        private String truncate(String message) {
            if (message == null) {
                return null;
            }
            int max = ragTraceProperties.getMaxErrorLength();
            return message.length() <= max ? message : message.substring(0, max);
        }
    }

    /**
     * token 估算：CJK 字符按 1 token、其余按 4 字符 1 token（标记 estimated=true，供后续 LLM usage 精确化）
     */
    private long estimateTokens(String text) {
        if (StrUtil.isBlank(text)) {
            return 0L;
        }
        int cjk = 0;
        int other = 0;
        for (char c : text.toCharArray()) {
            if (Character.UnicodeScript.of(c) == Character.UnicodeScript.HAN) {
                cjk++;
            } else {
                other++;
            }
        }
        return cjk + (other + 3) / 4;
    }
}
