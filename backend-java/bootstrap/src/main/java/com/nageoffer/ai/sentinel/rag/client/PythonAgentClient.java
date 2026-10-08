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

package com.nageoffer.ai.sentinel.rag.client;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.nageoffer.ai.sentinel.framework.context.UserContext;
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
 * Python agent 閫傞厤灞傚�㈡埛绔�锛氬皝瑁呭�� {@code agent-python} FastAPI 鏈嶅姟鐨勮皟鐢ㄣ�?
 *
 * <p>鏇夸唬鍘?sentinel 鏈�鍦� LLM / 妫�绱㈢紪鎺掞紝鎸?g2 鍒嗗眰鑱岃矗锛?
 * <ul>
 *   <li>{@link #streamChat} 杞�鍙� {@code POST /chat} 鐨?SSE 娴侊紙Java 澹抽�?token 閫忎紶锛?/li>
 *   <li>{@link #generateTitle} / {@link #recommend} / {@link #summarize} 杞�鍙� 3 涓�杈呭姪鎺ュ�?/li>
 * </ul>
 *
 * <p>鎵�鏈夋柟娉曞け璐ユ椂鎸夊師 sentinel 鐨勯檷绾ц��涔夎繑鍥烇紙鏍囬�樻埅鏂�/绌哄垪琛?鍘熸憳瑕侊級锛屼笉鎶涘嚭闃绘柇涓绘祦绋嬨�?
 */
@Slf4j
@Component
public class PythonAgentClient {

    private static final MediaType JSON = MediaType.parse("application/json; charset=utf-8");

    private final OkHttpClient syncHttpClient;
    private final OkHttpClient streamingHttpClient;
    private final ObjectMapper objectMapper;
    private final String baseUrl;
    private final String apiKey;

    public PythonAgentClient(
            OkHttpClient syncHttpClient,
            OkHttpClient streamingHttpClient,
            ObjectMapper objectMapper,
            @Value("${rag.python.base-url:http://localhost:8001}") String baseUrl,
            @Value("${rag.python.api-key:}") String apiKey
    ) {
        this.syncHttpClient = syncHttpClient;
        this.streamingHttpClient = streamingHttpClient;
        this.objectMapper = objectMapper;
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.apiKey = apiKey;
    }

    /**
     * 娴佸紡杞�鍙� {@code /chat}銆傞�?token 閫氳繃 {@code onToken} 鍥炶皟锛?
     * thinking 鍐呭�归�氳繃 {@code onThink} 鍥炶皟锛?
     * 娴佺粨鏉熸椂閫氳繃 {@code onCompleteData} 甯﹀洖瀹屾暣鏁版嵁锛坒ull_text/sources/grounding_chunks锛夛紝
     * 鍑洪敊閫氳繃 {@code onError} 閫氱煡銆?
     * 鍚屾�ラ樆濉炵洿鍒版祦缁撴潫锛堢敱璋冪敤鏂瑰湪寮傛�ョ嚎绋嬫墽琛岋級銆?
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
        Request request = withUserHeaders(new Request.Builder()
                .url(url)
                .post(RequestBody.create(toJson(body), JSON))
                .header("Accept", "text/event-stream")
                .build());

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
                    } else if ("complete".equals(type)) {
                        // Only the complete frame triggers the completion callback.
                        // The earlier sources frame carries the same payload; routing
                        // it here too would double-persist the assistant message.
                        Map<String, Object> payload = objectMapper.convertValue(node, new com.fasterxml.jackson.core.type.TypeReference<>() {});
                        onCompleteData.accept(payload);
                    } else if ("sources".equals(type)) {
                        // ignored: frontend sources panel is served by the FINISH frame
                    } else if ("end".equals(type)) {
                        done.complete(null);
                    } else if ("error".equals(type) || (node.has("success") && !node.get("success").asBoolean())) {
                        onError.accept(new RuntimeException("Python /chat 涓氬姟閿欒��: " + node.path("message").asText(node.path("code").asText("unknown"))));
                        done.complete(null);
                    }
                } catch (JsonProcessingException e) {
                    log.warn("瑙ｆ瀽 Python /chat SSE 甯уけ璐? {}", data, e);
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
            log.error("绛夊緟 Python /chat 娴佺粨鏉熻秴鏃?涓�鏂�", e);
            onError.accept(e);
        }
    }

    /**
     * 浼氳瘽鏍囬�樼敓鎴愩�傚け璐ラ檷绾т负闂�棰樻埅鏂�銆?
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
            log.warn("璋冪敤 Python /generate-title 澶辫触", e);
        }
        return (question == null ? "" : question).length() > 30
                ? (question == null ? "" : question).substring(0, 30)
                : (question == null ? "" : "");
    }

    /**
     * 鎺ㄨ崘杩介棶鐢熸垚銆傝繑鍥?null 琛ㄧず澶辫触锛堜繚鎸佷笌鍘?RecommendedQuestionsPayload.FAILED 涓�鑷寸敱璋冪敤鏂瑰�勭悊锛夈�?
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
            log.warn("璋冪敤 Python /recommend 澶辫触", e);
        }
        return null;
    }

    /**
     * 浼氳瘽璁板繂鎽樿�併�傚け璐ヨ繑鍥?existingSummary銆?
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
            log.warn("璋冪敤 Python /summarize 澶辫触", e);
        }
        return existingSummary == null ? "" : existingSummary;
    }

    /**
     * 鏈嶅姟绔�浠诲姟鍙栨秷锛氶�氱煡 Python agent 鍋滄�㈡寚瀹� task銆?
     */
    public void stopTask(String taskId) {
        try {
            Request request = withUserHeaders(new Request.Builder()
                    .url(baseUrl + "/stop/" + java.net.URLEncoder.encode(taskId, java.nio.charset.StandardCharsets.UTF_8))
                    .post(RequestBody.create("", JSON))
                    .build());
            try (Response response = syncHttpClient.newCall(request).execute()) {
                if (!response.isSuccessful()) {
                    log.warn("Python /stop 杩斿洖 HTTP {}", response.code());
                }
            }
        } catch (Exception e) {
            log.warn("璋冪敤 Python /stop 澶辫触 taskId={}", taskId, e);
        }
    }

    private JsonNode postJson(String path, Map<String, Object> body) throws Exception {
        Request request = withUserHeaders(new Request.Builder()
                .url(baseUrl + path)
                .post(RequestBody.create(toJson(body), JSON))
                .header("Content-Type", "application/json")
                .build());
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

    /**
     * 通用 JSON 转发（/case 管理面）：method/path/query/body 原样透传到 agent，
     * 出站自动带 X-API-Key + X-User-* 身份头。调用方负责关闭返回的 Response。
     */
    public Response forward(String method, String pathWithQuery, String jsonBody) throws Exception {
        Request.Builder builder = new Request.Builder().url(baseUrl + pathWithQuery);
        boolean hasBody = jsonBody != null && !jsonBody.isEmpty();
        switch (method.toUpperCase()) {
            case "GET" -> builder.get();
            case "DELETE" -> builder.delete();
            case "PUT" -> builder.put(RequestBody.create(hasBody ? jsonBody : "", JSON));
            default -> builder.post(RequestBody.create(hasBody ? jsonBody : "", JSON));
        }
        return syncHttpClient.newCall(withUserHeaders(builder.build())).execute();
    }

    /**
     * 流式转发（SSE 调查 / 报告导出）：返回 OkHttp Response，调用方读取
     * byteStream() 透传给浏览器，读毕关闭 Response。
     */
    public Response forwardStream(String method, String pathWithQuery, String jsonBody) throws Exception {
        Request.Builder builder = new Request.Builder().url(baseUrl + pathWithQuery);
        if ("GET".equalsIgnoreCase(method)) {
            builder.get();
        } else {
            builder.post(RequestBody.create(jsonBody == null ? "" : jsonBody, JSON));
        }
        builder.header("Accept", "text/event-stream");
        return streamingHttpClient.newCall(withUserHeaders(builder.build())).execute();
    }

    /**
     * 出站请求统一注入：X-API-Key（服务间认证）+ X-User-Name/X-User-Role（操作人身份，
     * 取自网关 UserContext TTL；未登录则不加身份头，agent 侧敏感操作会 403）。
     */
    private Request withUserHeaders(Request request) {
        String username = UserContext.getUsername();
        String role = UserContext.getRole();
        Request.Builder builder = request.newBuilder();
        if (apiKey != null && !apiKey.isBlank()) {
            builder.header("X-API-Key", apiKey);
        }
        if (username != null && !username.isBlank()) {
            builder.header("X-User-Name", username);
        }
        if (role != null && !role.isBlank()) {
            builder.header("X-User-Role", role);
        }
        return builder.build();
    }

    private String toJson(Object body) {
        try {
            return objectMapper.writeValueAsString(body);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("搴忓垪鍖栬�锋眰浣撳け璐�", e);
        }
    }
}
