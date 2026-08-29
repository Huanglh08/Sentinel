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

package com.nageoffer.ai.sentinel.core.ingest.sink;

import com.nageoffer.ai.sentinel.core.chunk.model.EmbeddedChunk;
import com.nageoffer.ai.sentinel.core.ingest.DocumentRef;
import com.nageoffer.ai.sentinel.core.ingest.VectorTarget;

import java.util.List;

/**
 * 索引落点端口：内核只认这个接口，实现住在各自模块里
 * <p>
 * 内核注入 {@code List<ChunkSink>} 扇出，实现间先后由 Spring 的 {@code @Order} 决定，全部包在内核落库步骤的同一个事务里；
 * 鍙�鏆撮�?鏁翠綋鏇挎崲"鑰岄潪鍒?+ 鍐欎袱涓�鏂规硶锛屽厛鍒犲悗寤虹殑椤哄簭鐢卞疄鐜拌嚜宸变繚璇侊紙鍚戦噺瑁呴グ鍣ㄩ摼闈犲畠鏋勬�?upsert 璇�涔夛�?
 */
public interface ChunkSink {

    /**
     * 用给定的块整体替换该文档已有的块，空列表表示该文档不产生任何块
     */
    void replaceDocument(VectorTarget target, DocumentRef doc, List<EmbeddedChunk> chunks);

    /**
     * 清除该文档的全部块
     */
    void deleteDocument(VectorTarget target, DocumentRef doc);
}
