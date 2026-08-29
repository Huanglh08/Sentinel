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

/**
 * 鎵归噺涓婁紶鍗曚釜鏂囦欢鐨勭粨鏋?
 */
@Data
public class BatchUploadItemVO {

    /**
     * 鍘熷�嬫枃浠跺�?
     */
    private String fileName;

    /**
     * 鏂囨。 ID锛堟垚鍔熸椂杩斿洖锛?
     */
    private String docId;

    /**
     * 澶勭悊鐘舵�侊細success / failed
     */
    private String status;

    /**
     * 澶辫触鏃剁殑閿欒��淇℃伅
     */
    private String error;
}
