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

package com.nageoffer.ai.sentinel.framework.trace;

import org.springframework.stereotype.Component;

/**
 * {@link RagStreamTraceSupport} 鐨勯粯璁ょ┖瀹炵幇锛坣o-op锛夈�?
 * <p>
 * 鐢ㄤ簬搴旂敤鍚�鍔ㄦ椂涓嶄緷璧栫湡瀹� Trace 瀛樺偍鍗冲彲杩愯�岋紱鎺ュ�?{@code t_rag_trace_node}
 * 鎸佷箙鍖栨椂鍐嶆浛鎹�涓哄彲鍐欏簱鐨勫疄鐜般�?
 */
@Component
public class NoopRagStreamTraceSupport implements RagStreamTraceSupport {

    @Override
    public StreamSpan beginStreamNode(String name, String type) {
        return NOOP_SPAN;
    }
}
