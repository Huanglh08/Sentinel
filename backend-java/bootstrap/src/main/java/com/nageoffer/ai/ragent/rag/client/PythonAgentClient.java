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

package com.nageoffer.ai.ragent.rag.client;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.sse.EventSource;
import okhttp3.sse.EventSourceListener;
import okhttp3.sse.EventSources;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/**
 * Python agent 适配层客户端：封装对 {@code agent-python} FastAPI 服务的调用。
 *
 * <p>替代原 ragent 本地 LLM / 检索编排，按 g2 分层职责：
 * <ul>
 *   <li>{@link #streamChat} 转发 {@code POST /chat} 的 SSE 流（Java 壳逐 token 透传）</li>
 *   <li>{@link #generateTitle} / {@link #recommend} / {@link #summarize} 转发 3 个辅助接口</li>
 * </ul>
 *
 * <p>所有方法失败时按原 ragent 的降级语义返回（标题截断/空列表/原摘要），不抛出阻断主流程。
 */
@Slf4j
@Component
public class PythonAgentClient {

    private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    private final OkHttpClient syncHttpClient;
    private final OkHttpClient streamingHttpClient;
    private final ObjectMapper objectMapper;
    private final String baseUrl;

    public PythonAgentClient(
            OkHttpClient syncHttpClient,
            OkHttpClient streamingHttpClient,
            ObjectMapper objectMapper,
            @Value("${ragent.python.base-url:http://localhost:8000}") String baseUrl
    ) {
        this.syncHttpClient = syncHttpClient;
        this.streamingHttpClient = streamingHttpClient;
        this.objectMapper = objectMapper;
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
    }

    /**
     * 流式转发 {@code /chat}。逐 token 通过 {@code onToken} 回调，
     * thinking 内容通过 {@code onThink} 回调，
     * 流结束时通过 {@code onCompleteData} 带回完整数据（full_text/sources/grounding_chunks），
     * 出错通过 {@code onError} 通知。
     * 同步阻塞直到流结束（由调用方在异步线程执行）。
     */
    public void streamChat(
            String question,
            String conversationId,
            String taskId,
            List<String> knowledgeBaseIds,
            List<Map<String, String>> history,
            Consumer<String> onToken,
            Consumer<String> onThink,
            Consumer<Map<String, Object>> onCompleteData,
            Consumer<Throwable> onError
    ) {
        HttpUrl url = HttpUrl.get(baseUrl + "/chat");
        Map<String, Object> body = new java.util.LinkedHashMap<>();
        body.put("question", question);
        body.put("conversationId", conversationId);
        body.put("taskId", taskId);
        body.put("knowledgeBaseIds", knowledgeBaseIds == null ? List.of() : knowledgeBaseIds);
        body.put("history", history == null ? List.of() : history);
        Request request = new Request.Builder()
                .url(url)
                .post(RequestBody.create(toJson(body), JSON))
                .header("Accept", "text/event-stream")
                .build();

        CompletableFuture<Void> done = new CompletableFuture<>();
        EventSourceListener listener = new EventSourceListener() {
            @Override
            public void onOpen(EventSource eventSource, Response response) {
            }

            @Override
            public void onEvent(EventSource eventSource, String id, String type, String data) {
                try {
                    JsonNode node = objectMapper.readTree(data);
                    if ("message".equals(type) && node.has("delta")) {
                        String delta = node.get("delta").asText();
                        if (!delta.isEmpty()) {
                            onToken.accept(delta);
                        }
                    } else if ("think".equals(type) && node.has("delta")) {
                        String delta = node.get("delta").asText();
                        if (!delta.isEmpty()) {
                            onThink.accept(delta);
                        }
                    } else if ("sources".equals(type) || "complete".equals(type)) {
                        Map<String, Object> payload = objectMapper.convertValue(node, new com.fasterxml.jackson.core.type.TypeReference<>() {});
                        onCompleteData.accept(payload);
                    } else if ("end".equals(type)) {
                        done.complete(null);
                    } else if ("error".equals(type) || (node.has("success") && !node.get("success").asBoolean())) {
                        onError.accept(new RuntimeException("Python /chat 业务错误: " + node.path("message").asText(node.path("code").asText("unknown"))));
                        done.complete(null);
                    }
                } catch (JsonProcessingException e) {
                    log.warn("解析 Python /chat SSE 帧失败: {}", data, e);
                }
            }

            @Override
            public void onClosed(EventSource eventSource) {
                if (!done.isDone()) {
                    done.complete(null);
                }
            }

            @Override
            public void onFailure(EventSource eventSource, Throwable t, Response response) {
                if (!done.isDone()) {
                    onError.accept(t);
                    done.complete(null);
                }
            }
        };

        EventSources.createFactory(streamingHttpClient)
                .newEventSource(request, listener);

        try {
            done.get(30, TimeUnit.MINUTES);
        } catch (Exception e) {
            log.error("等待 Python /chat 流结束超时/中断", e);
            onError.accept(e);
        }
    }

    /**
     * 会话标题生成。失败降级为问题截断。
     */
    public String generateTitle(String question) {
        try {
            Map<String, Object> body = Map.of("question", question == null ? "" : question);
            JsonNode node = postJson("/generate-title", body);
            String title = node.path("title").asText("").trim();
            if (!title.isEmpty()) {
                return title.length() > 40 ? title.substring(0, 40) : title;
            }
        } catch (Exception e) {
            log.warn("调用 Python /generate-title 失败", e);
        }
        return (question == null ? "" : question).length() > 30
                ? (question == null ? "" : question).substring(0, 30)
                : (question == null ? "" : "");
    }

    /**
     * 推荐追问生成。返回 null 表示失败（保持与原 RecommendedQuestionsPayload.FAILED 一致由调用方处理）。
     */
    public List<String> recommend(String question, String answer, List<Map<String, String>> chunks) {
        try {
            Map<String, Object> body = new java.util.LinkedHashMap<>();
            body.put("question", question == null ? "" : question);
            body.put("answer", answer == null ? "" : answer);
            body.put("chunks", chunks == null ? List.of() : chunks);
            JsonNode node = postJson("/recommend", body);
            JsonNode questions = node.path("questions");
            if (questions.isArray()) {
                java.util.List<String> result = new java.util.ArrayList<>();
                for (JsonNode q : questions) {
                    result.add(q.asText());
                }
                return result;
            }
        } catch (Exception e) {
            log.warn("调用 Python /recommend 失败", e);
        }
        return null;
    }

    /**
     * 会话记忆摘要。失败返回 existingSummary。
     */
    public String summarize(List<Map<String, String>> messages, String existingSummary) {
        try {
            Map<String, Object> body = new java.util.LinkedHashMap<>();
            body.put("messages", messages == null ? List.of() : messages);
            body.put("existing_summary", existingSummary == null ? "" : existingSummary);
            JsonNode node = postJson("/summarize", body);
            String summary = node.path("summary").asText("").trim();
            if (!summary.isEmpty()) {
                return summary;
            }
        } catch (Exception e) {
            log.warn("调用 Python /summarize 失败", e);
        }
        return existingSummary == null ? "" : existingSummary;
    }

    /**
     * 服务端任务取消：通知 Python agent 停止指定 task。
     */
    public void stopTask(String taskId) {
        try {
            Request request = new Request.Builder()
                    .url(baseUrl + "/stop/" + java.net.URLEncoder.encode(taskId, java.nio.charset.StandardCharsets.UTF_8))
                    .post(RequestBody.create("", JSON))
                    .build();
            try (Response response = syncHttpClient.newCall(request).execute()) {
                if (!response.isSuccessful()) {
                    log.warn("Python /stop 返回 HTTP {}", response.code());
                }
            }
        } catch (Exception e) {
            log.warn("调用 Python /stop 失败 taskId={}", taskId, e);
        }
    }

    private JsonNode postJson(String path, Map<String, Object> body) throws Exception {
        Request request = new Request.Builder()
                .url(baseUrl + path)
                .post(RequestBody.create(toJson(body), JSON))
                .header("Content-Type", "application/json")
                .build();
        try (Response response = syncHttpClient.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                String err = "";
                try (ResponseBody rb = response.body()) {
                    err = rb == null ? "" : rb.string();
                }
                throw new RuntimeException("Python " + path + " HTTP " + response.code() + ": " + err);
            }
            try (ResponseBody rb = response.body()) {
                String text = rb == null ? "{}" : rb.string();
                return objectMapper.readTree(text);
            }
        }
    }

    private String toJson(Object body) {
        try {
            return objectMapper.writeValueAsString(body);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("序列化请求体失败", e);
        }
    }
}
