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

package com.nageoffer.ai.sentinel.knowledge.support;

import com.nageoffer.ai.sentinel.core.ingest.VectorTarget;
import com.nageoffer.ai.sentinel.framework.exception.ClientException;
import com.nageoffer.ai.sentinel.knowledge.dao.entity.KnowledgeBaseDO;
import com.nageoffer.ai.sentinel.rag.config.RAGDefaultProperties;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

/**
 * 向量落点派生：知识库配置（L2）+ 部署配置（L1）→ {@link VectorTarget}
 * <p>
 * 鍗曠嫭鎴愪竴涓�缁勪欢鏄�涓轰簡璁?钀界偣韬�浠芥�庝箞绠楀嚭鏉?鍙�鏈変竴涓�浜х敓鍦般�傚師鍏堟瘡涓�鍐欏悜閲忕殑璋冪敤鐐瑰悇鑷�浠庣煡璇嗗簱
 * 取模型、各自决定要不要回落系统默认，于是上传路径用知识库配置的模型、管道路径用系统默认模型，
 * 同一个分区里混进了两种语义空间的向量
 */
@Component
@RequiredArgsConstructor
public class VectorTargetResolver {

    private final RAGDefaultProperties ragDefaultProperties;

    /**
     * 派生落点，缺配置直接失败而不是回落默认值
     */
    public VectorTarget resolve(KnowledgeBaseDO kbDO) {
        if (kbDO == null) {
            throw new ClientException("知识库不存在");
        }
        if (!StringUtils.hasText(kbDO.getEmbeddingModel())) {
            throw new ClientException("知识库未配置嵌入模型：kbId=" + kbDO.getId());
        }
        Integer dimension = ragDefaultProperties.getDimension();
        if (dimension == null || dimension <= 0) {
            throw new ClientException("部署未配置向量维度 rag.default.dimension");
        }
        return new VectorTarget(kbDO.getCollectionName(), kbDO.getEmbeddingModel(), dimension);
    }
}
