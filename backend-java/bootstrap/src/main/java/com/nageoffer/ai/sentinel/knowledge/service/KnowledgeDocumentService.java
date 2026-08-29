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

package com.nageoffer.ai.sentinel.knowledge.service;

import com.baomidou.mybatisplus.core.metadata.IPage;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.nageoffer.ai.sentinel.knowledge.controller.request.KnowledgeDocumentPageRequest;
import com.nageoffer.ai.sentinel.knowledge.controller.request.KnowledgeDocumentUploadRequest;
import com.nageoffer.ai.sentinel.knowledge.controller.request.KnowledgeDocumentUpdateRequest;
import com.nageoffer.ai.sentinel.knowledge.controller.vo.KnowledgeDocumentVO;
import com.nageoffer.ai.sentinel.knowledge.controller.vo.KnowledgeDocumentChunkLogVO;
import com.nageoffer.ai.sentinel.knowledge.controller.vo.KnowledgeDocumentSearchVO;
import com.nageoffer.ai.sentinel.knowledge.controller.vo.BatchUploadItemVO;
import com.nageoffer.ai.sentinel.knowledge.controller.vo.BatchUploadResponseVO;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

/**
 * 鐭ヨ瘑搴撴枃妗ｆ湇鍔℃帴鍙?
 */
public interface KnowledgeDocumentService {

    /**
     * 涓婁紶鏂囨。
     *
     * @param kbId         鐭ヨ瘑搴?ID
     * @param requestParam 璇锋眰瀵硅薄鍙傛暟
     * @param file         寰呬笂浼犵殑鏂囦欢
     * @return 知识库文档视图对象
     */
    KnowledgeDocumentVO upload(String kbId, KnowledgeDocumentUploadRequest requestParam, MultipartFile file);

    /**
     * 开始文档分片处理（校验状态并发送 MQ 消息，立即返回）
     *
     * @param docId 鏂囨。 ID
     */
    void startChunk(String docId);

    /**
     * 执行文档分块（由 MQ 消费者调用）
     * 鑾峰彇鍒嗗竷寮忛攣 鈫?娓呯悊鍘嗗彶鍒嗗潡鍜屽悜閲?鈫?鎵ц�屽畬鏁村垎鍧楁祦绋�
     *
     * @param docId 鏂囨。 ID
     */
    void executeChunk(String docId);

    /**
     * 鍒犻櫎鏂囨。
     *
     * @param docId 鏂囨。 ID
     */
    void delete(String docId);

    /**
     * 鑾峰彇鏂囨。璇︽儏
     *
     * @param docId 鏂囨。 ID
     * @return 知识库文档视图对象
     */
    KnowledgeDocumentVO get(String docId);

    /**
     * 鏇存柊鏂囨。淇℃伅
     *
     * @param docId        鏂囨。 ID
     * @param requestParam 鏇存柊璇锋眰鍙傛暟
     */
    void update(String docId, KnowledgeDocumentUpdateRequest requestParam);

    /**
     * 分页查询文档
     *
     * @param kbId         鐭ヨ瘑搴?ID
     * @param requestParam 筛选参数
     * @return 鏂囨。鍒嗛〉缁撴灉
     */
    IPage<KnowledgeDocumentVO> page(String kbId, KnowledgeDocumentPageRequest requestParam);

    /**
     * 启用或禁用文档
     *
     * @param docId   鏂囨。 ID
     * @param enabled 是否启用
     */
    void enable(String docId, boolean enabled);

    /**
     * 搜索文档（用于全局检索建议）
     *
     * @param keyword 鍏抽敭璇?
     * @param limit   最大返回数量
     * @return 鏂囨。鍒楄〃
     */
    List<KnowledgeDocumentSearchVO> search(String keyword, int limit);

    /**
     * 查询文档分块日志
     *
     * @param docId 鏂囨。 ID
     * @param page  鍒嗛〉鍙傛暟
     * @return 鍒嗗潡鏃ュ織鍒嗛〉缁撴灉
     */
    IPage<KnowledgeDocumentChunkLogVO> getChunkLogs(String docId, Page<KnowledgeDocumentChunkLogVO> page);

    /**
     * 预览 markdown 文档内容
     *
     * @param docId 鏂囨。 ID
     * @return markdown 原始文本内容
     */
    String preview(String docId);

    /**
     * 鎵归噺涓婁紶鏂囨。銆傞�愪釜澶嶇敤 {@link #upload(String, KnowledgeDocumentUploadRequest, MultipartFile)}锛?
     * 鍗曚釜鏂囦欢澶辫触涓嶅奖鍝嶅叾浠栨枃浠讹紝鏈�缁堟眹鎬绘垚鍔?澶辫触鍒楄〃銆?
     *
     * @param kbId         鐭ヨ瘑搴?ID
     * @param requestParam 澶嶇敤鍗曟枃浠朵笂浼犵殑璇锋眰鍙傛暟
     * @param files        寰呬笂浼犵殑鏂囦欢鍒楄〃锛岃嚦灏?1 涓?
     * @return 鎵归噺涓婁紶缁撴灉锛屽寘鍚�姣忎釜鏂囦欢鐨勭姸鎬?
     */
    BatchUploadResponseVO batchUpload(String kbId, KnowledgeDocumentUploadRequest requestParam, List<MultipartFile> files);
}
