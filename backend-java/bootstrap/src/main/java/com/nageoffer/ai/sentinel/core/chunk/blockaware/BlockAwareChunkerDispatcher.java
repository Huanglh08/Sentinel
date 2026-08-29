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

package com.nageoffer.ai.sentinel.core.chunk.blockaware;

import com.nageoffer.ai.sentinel.core.chunk.model.Chunk;
import com.nageoffer.ai.sentinel.core.chunk.model.ChunkAssembler;
import com.nageoffer.ai.sentinel.core.chunk.model.ChunkBudget;
import com.nageoffer.ai.sentinel.core.chunk.model.ChunkDraft;
import com.nageoffer.ai.sentinel.core.parser.model.Block;
import com.nageoffer.ai.sentinel.core.parser.model.HeadingBlock;
import com.nageoffer.ai.sentinel.framework.exception.ServiceException;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 分块调度器：Block 类型 → chunker 查表分发，同一类型被两个 chunker 认领时启动即失败
 * <p>
 * 标题先更新章节路径再照常分发，于是它拿到的是含自己在内的路径，与其后正文同节而自然同块；
 * 娴佺▼鍥哄畾涓哄垎鍙戜骇鑽夌�� 鈫?鎸夎妭鎵撳寘 鈫?缁熶竴瑁呴厤锛岃�呴厤鐣欏湪鏈�绔�鏄�鍥犱负鍚戦噺鏂囨湰瑕佹嫾绔犺妭鍓嶇紑锛?
 * 而打包只能发生在拼前缀之前
 */
@Component
public class BlockAwareChunkerDispatcher {

    private final HeadingHandler headingHandler;
    private final ChunkPacker chunkPacker;
    private final Map<Class<? extends Block>, BlockChunker<?>> registry;

    public BlockAwareChunkerDispatcher(HeadingHandler headingHandler,
                                       ChunkPacker chunkPacker,
                                       List<BlockChunker<?>> chunkers) {
        this.headingHandler = headingHandler;
        this.chunkPacker = chunkPacker;
        Map<Class<? extends Block>, BlockChunker<?>> table = new HashMap<>();
        for (BlockChunker<?> chunker : chunkers) {
            BlockChunker<?> previous = table.put(chunker.blockType(), chunker);
            if (previous != null) {
                throw new ServiceException(String.format(
                        "Block 分块器注册冲突：类型=%s 同时被 %s 与 %s 认领",
                        chunker.blockType().getSimpleName(),
                        previous.getClass().getSimpleName(), chunker.getClass().getSimpleName()));
            }
        }
        this.registry = Map.copyOf(table);
    }

    /**
     * 把 Block 列表切分为有序块，序号从 0 单调递增
     */
    public List<Chunk> dispatch(List<Block> blocks, ChunkBudget budget) {
        if (blocks == null || blocks.isEmpty()) {
            return List.of();
        }

        HeadingHandler.Outline outline = HeadingHandler.Outline.EMPTY;
        List<ChunkDraft> drafts = new ArrayList<>();
        for (Block block : blocks) {
            if (block instanceof HeadingBlock heading) {
                outline = headingHandler.update(outline, heading);
            }
            drafts.addAll(chunkOne(block, ChunkContext.of(outline.path(), budget)));
        }

        return ChunkAssembler.assembleAll(chunkPacker.pack(drafts, budget));
    }

    @SuppressWarnings("unchecked")
    private List<ChunkDraft> chunkOne(Block block, ChunkContext ctx) {
        BlockChunker<Block> chunker = (BlockChunker<Block>) registry.get(block.getClass());
        if (chunker == null) {
            throw new ServiceException("没有 chunker 认领 Block 类型：" + block.getClass().getName());
        }
        return chunker.chunk(block, ctx);
    }
}
