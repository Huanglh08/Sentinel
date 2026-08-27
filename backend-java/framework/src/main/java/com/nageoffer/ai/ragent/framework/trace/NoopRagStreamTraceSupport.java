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

package com.nageoffer.ai.ragent.framework.trace;

import org.springframework.stereotype.Component;

/**
 * {@link RagStreamTraceSupport} 的默认空实现（no-op）。
 * <p>
 * 用于应用启动时不依赖真实 Trace 存储即可运行；接入 {@code t_rag_trace_node}
 * 持久化时再替换为可写库的实现。
 */
@Component
public class NoopRagStreamTraceSupport implements RagStreamTraceSupport {

    @Override
    public StreamSpan beginStreamNode(String name, String type) {
        return NOOP_SPAN;
    }
}
