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

package com.nageoffer.ai.ragent.knowledge.controller.vo;

import lombok.Data;

/**
 * 批量上传单个文件的结果
 */
@Data
public class BatchUploadItemVO {

    /**
     * 原始文件名
     */
    private String fileName;

    /**
     * 文档 ID（成功时返回）
     */
    private String docId;

    /**
     * 处理状态：success / failed
     */
    private String status;

    /**
     * 失败时的错误信息
     */
    private String error;
}
