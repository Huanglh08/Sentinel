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

package com.nageoffer.ai.sentinel.rag.util;

import cn.hutool.core.util.StrUtil;

import java.util.regex.Pattern;

/**
 * 鍥炵瓟琛屽唴寮曠敤鏍囪�板伐鍏�
 * <p>
 * 寮曠敤闅忓洖绛旀�ｆ枃鎸佷箙鍖栦互淇濇寔浣嶇疆绋冲畾锛屼絾杩涘叆涓嬩竴杞�妯″瀷鍘嗗彶鎴栨帹鑽愰棶棰樼敓鎴愭椂搴旂Щ闄わ�?
 * 閬垮厤涓婁竴杞�鐨勫眬閮ㄧ紪鍙锋薄鏌撴湰杞�寮曠敤缂栧�?
 */
public final class CitationMarkup {

    /**
     * 鍖归厤绯荤粺瀹氫箟鐨勮�屽唴寮曠敤鏍煎紡锛�
     * [1](#cite-1)銆乕10](#cite-10)
     * <p>
     * 涓嶅己鍒舵樉绀虹紪鍙蜂笌閿氱偣缂栧彿涓�鑷达紝鍥犱负娓呯悊閫昏緫闇�瑕佸吋瀹规ā鍨嬪伓灏斾骇鐢熺殑閿欒��鏍煎紡锛?
     * 渚嬪�� [1](#cite-2) 涔熷簲浠庝笅涓�杞�涓婁笅鏂囦腑绉婚櫎銆?
     */
    private static final Pattern INLINE_CITATION =
            Pattern.compile("\\[[1-9]\\d*]\\(#cite-[1-9]\\d*\\)");

    private CitationMarkup() {
    }

    public static String strip(String content) {
        if (StrUtil.isBlank(content)) {
            return "";
        }
        return INLINE_CITATION.matcher(content).replaceAll("");
    }
}
