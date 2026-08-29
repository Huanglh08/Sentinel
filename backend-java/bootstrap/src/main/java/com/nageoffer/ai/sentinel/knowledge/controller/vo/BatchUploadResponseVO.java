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

package com.nageoffer.ai.sentinel.knowledge.controller.vo;

import lombok.Data;

import java.util.List;

/**
 * 鎵归噺涓婁紶杩斿洖缁撴灉
 */
@Data
public class BatchUploadResponseVO {

    /**
     * 鎴愬姛涓婁紶鏁伴噺
     */
    private int success;

    /**
     * 澶辫触鏁伴噺
     */
    private int failed;

    /**
     * 姣忎釜鏂囦欢鐨勫�勭悊缁撴�?
     */
    private List<BatchUploadItemVO> items;
}
