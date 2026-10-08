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

import com.nageoffer.ai.sentinel.rag.client.PythonAgentClient;
import jakarta.servlet.http.HttpServletRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import java.nio.charset.StandardCharsets;

/**
 * 案件调查代理控制器：把前端 /case/** 与 /cases 请求转发到 Python agent（8001）。
 *
 * <p>鉴权链路：Sa-Token 全局拦截器先做登录校验并填充 UserContext，
 * {@link PythonAgentClient} 出站时注入 X-API-Key + X-User-Name/X-User-Role，
 * agent 侧凭身份头执行审批双人复核与 admin 角色校验——浏览器无法伪造身份。
 *
 * <p>agent 返回裸 JSON（无 code 包装），前端 axios 拦截器对无 code 结构原样透传，
 * 因此本层不做 Result 包装，错误体将 agent 的 {"detail":...} 归一化为 {"message":...}。
 */
@Slf4j
@RestController
@RequiredArgsConstructor
public class CaseProxyController {

    private final PythonAgentClient pythonAgentClient;

    /**
     * 发起调查（SSE 逐事件透传证据链）。前端用 fetch + reader 消费，
     * 事件流格式与 agent 直连完全一致。
     */
    @org.springframework.web.bind.annotation.PostMapping("/case/investigate")
    public ResponseEntity<StreamingResponseBody> investigate(HttpServletRequest request) {
        String path = agentPath(request);
        String body = readBody(request);
        return streamTransfer("POST", path, body, MediaType.TEXT_EVENT_STREAM);
    }

    /**
     * 调查报告导出（Markdown blob 下载流式透传）。
     */
    @org.springframework.web.bind.annotation.GetMapping("/case/{caseId}/report/export")
    public ResponseEntity<StreamingResponseBody> exportReport(HttpServletRequest request) {
        return streamTransfer("GET", agentPath(request), "", MediaType.APPLICATION_OCTET_STREAM);
    }

    /**
     * 其余 /case/** JSON 端点（详情/关闭/审计/风险策略/审批列表/裁决/巡检/规则）。
     * Spring 优先匹配上面两个更具体的映射，不会路由到这里。
     */
    @RequestMapping(value = "/case/**", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> proxyCase(HttpServletRequest request) {
        return jsonTransfer(request, agentPath(request), readBody(request));
    }

    /**
     * 案件列表（agent 的 /cases，注意不是 /case 前缀）。
     */
    @org.springframework.web.bind.annotation.GetMapping("/cases")
    public ResponseEntity<String> proxyCases(HttpServletRequest request) {
        return jsonTransfer(request, "/cases" + withQuery(request), "");
    }

    private ResponseEntity<String> jsonTransfer(HttpServletRequest request, String path, String body) {
        String method = request.getMethod();
        try (Response response = pythonAgentClient.forward(method, path, body)) {
            int status = response.code();
            ResponseBody rb = response.body();
            String text = rb == null ? "{}" : rb.string().trim();
            if (text.isEmpty()) {
                text = "{}";
            }
            if (!response.isSuccessful()) {
                // agent 错误体是 FastAPI 的 {"detail": "..."}，归一化给前端 toast
                text = normalizeError(text);
                log.warn("[case-proxy] {} {} -> HTTP {} {}", method, path, status, text);
            }
            return ResponseEntity.status(status)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(text);
        } catch (Exception e) {
            log.error("[case-proxy] {} {} 转发失败", method, path, e);
            return ResponseEntity.status(502)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body("{\"message\":\"调查服务暂不可用，请稍后重试\"}");
        }
    }

    private ResponseEntity<StreamingResponseBody> streamTransfer(String method, String path, String body, MediaType fallback) {
        Response response;
        try {
            response = pythonAgentClient.forwardStream(method, path, body);
        } catch (Exception e) {
            log.error("[case-proxy] {} {} 流式转发建立失败", method, path, e);
            return errorStream(502, "调查服务暂不可用，请稍后重试");
        }
        if (!response.isSuccessful()) {
            String text;
            try (ResponseBody rb = response.body()) {
                text = rb == null ? "{}" : normalizeError(rb.string().trim());
            } catch (Exception e) {
                text = "上游服务错误";
            }
            log.warn("[case-proxy] {} {} -> HTTP {} {}", method, path, response.code(), text);
            response.close();
            return errorStream(response.code(), text);
        }
        okhttp3.MediaType ok = response.body() != null ? response.body().contentType() : null;
        MediaType contentType = ok != null ? MediaType.parseMediaType(ok.toString()) : fallback;
        StreamingResponseBody stream = out -> {
            try (ResponseBody rb = response.body()) {
                if (rb != null) {
                    try (var in = rb.byteStream()) {
                        in.transferTo(out);
                    }
                }
            } finally {
                response.close();
            }
        };
        return ResponseEntity.ok().contentType(contentType).body(stream);
    }

    /** 错误以 JSON 体流式返回（message 归一化，前端 toast 可读）。 */
    private ResponseEntity<StreamingResponseBody> errorStream(int status, String message) {
        final String payload = "{\"message\": " + jsonEscape(message) + "}";
        StreamingResponseBody stream = out -> out.write(payload.getBytes(StandardCharsets.UTF_8));
        return ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON).body(stream);
    }

    /**
     * 提取 agent 目标路径：context-path（/api/sentinel）剥离后的 URI + query。
     * 如 /api/sentinel/case/123?with=audit -> /case/123?with=audit
     */
    private String agentPath(HttpServletRequest request) {
        String uri = request.getRequestURI();
        String contextPath = request.getContextPath();
        if (contextPath != null && !contextPath.isEmpty() && uri.startsWith(contextPath)) {
            uri = uri.substring(contextPath.length());
        }
        return uri + withQuery(request);
    }

    private String withQuery(HttpServletRequest request) {
        String query = request.getQueryString();
        return (query == null || query.isEmpty()) ? "" : "?" + query;
    }

    private String readBody(HttpServletRequest request) {
        try (var in = request.getInputStream()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    private String normalizeError(String agentBody) {
        // {"detail": "..."} -> {"message": "..."}；其余结构原样透传
        var m = java.util.regex.Pattern.compile("^\\s*\\{\\s*\"detail\"\\s*:\\s*\"(.*)\"\\s*\\}\\s*$")
                .matcher(agentBody);
        if (m.matches()) {
            return "{\"message\":" + jsonEscape(m.group(1)) + "}";
        }
        return agentBody;
    }

    private String jsonEscape(String raw) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(raw);
        } catch (Exception e) {
            return "\"" + raw.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
        }
    }
}
