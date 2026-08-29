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

package com.nageoffer.ai.sentinel.admin.controller.vo;

import lombok.Builder;
import lombok.Data;

import java.util.List;

@Data
@Builder
public class DashboardPerformanceVO {

    private String window;

    private Long avgLatencyMs;

    private Long p95LatencyMs;

    private Double successRate;

    private Double errorRate;

    private Double noDocRate;

    private Double slowRate;

    // ---------- v3 新增指标 ----------

    /** 用户感知首包延迟（TTFT）平均值（毫秒） */
    private Long avgTtftMs;

    /** 用户感知首包延迟（TTFT）P95（毫秒） */
    private Long p95TtftMs;

    /** 生成速度（tokens/s）平均值 */
    private Double avgTps;

    /** 输出 token 总量（估算） */
    private Long outputTokens;

    /** 自主完成率（%）：无需澄清/拒答/空检索即给出有效回答的比例 */
    private Double autonomyRate;

    /** 拒答率（%）：风控 agent 拦截面 */
    private Double refusalRate;

    /** 工具调用成功率（%）：TOOL 节点成功占比 */
    private Double toolCallSuccessRate;

    /** 点赞数 */
    private Long likeCount;

    /** 点踩数 */
    private Long dislikeCount;

    /** 好评率（%） */
    private Double likeRate;

    /** 风险类别分布（sources 命中 LLM01~LLM10 计数） */
    private List<DashboardNameCountVO> riskCategories;

    /** 回答结果分布（autonomous / refusal / no_doc / no_retrieval 计数） */
    private List<DashboardNameCountVO> outcomeDistribution;
}
