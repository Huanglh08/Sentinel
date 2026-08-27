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

import java.util.List;

/**
 * 批量上传返回结果
 */
@Data
public class BatchUploadResponseVO {

    /**
     * 成功上传数量
     */
    private int success;

    /**
     * 失败数量
     */
    private int failed;

    /**
     * 每个文件的处理结果
     */
    private List<BatchUploadItemVO> items;
}
