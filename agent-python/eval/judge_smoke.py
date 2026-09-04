"""Judge 冒烟：用 3 个已知答案的样本验证 judge 模型是否可用、判分是否靠谱。

作用（两件事一起验）：
1. **通道**：API Key、模型名、base_url、JSON 结构化输出是否通。
2. **准确性**：rubric 能否把"明显幻觉""忠实回答""挂羊头引用"区分开——
   这是 judge 一致率能否过 90% 的前置信号。

运行：
    cd agent-python
    python -m eval.judge_smoke                 # 跑默认 3 个样本
    python -m eval.judge_smoke --model deepseek-v4-pro   # 对比仲裁档

退出码：0 = 全部符合预期；1 = 有样本判反（说明该模型/rubric 组合不可用）。
"""
from __future__ import annotations

import argparse
import asyncio
import json

from eval.judge import JUDGE_TASKS, JudgeClient, build_user_prompt, normalize_result
from eval.judge.config import JudgeConfig

# ---------------- 样本：答案已知，用于检验 judge 判得对不对 ----------------

CTX_MFA = [
    {
        "doc_id": "Multifactor_Authentication_Cheat_Sheet",
        "text": (
            "多因素认证（MFA）建议在以下场景启用：登录、敏感操作（改密、改绑手机、提现）、"
            "以及风险设备首次登录。实现方式优先级：WebAuthn/硬件密钥 > TOTP 应用 > 短信。"
            "短信验证码因存在 SIM 交换与拦截风险，不建议作为唯一因素。"
            "恢复码应一次性使用、不可枚举，并在使用后失效。"
        ),
    }
]

# 样本 1：明显幻觉（资料里没有阈值与 90 天，也没说"必须"）
S_HALLUCINATION = {
    "task": "faithfulness",
    "question": "我们系统的 MFA 应该怎么配？",
    "contexts": CTX_MFA,
    "answer": (
        "你们系统必须强制开启 MFA，风险评分阈值设定为 0.85，短信验证码有效期 60 秒，"
        "并且要求所有用户每 90 天更换一次密码。不满足的话一律拒绝登录。"
    ),
    "expect": "< 0.6",
    "check": lambda r: r["score"] < 0.6,
    "why": "阈值 0.85、60 秒有效期、90 天改密、'一律拒绝'均为资料中不存在的编造",
}

# 样本 2：忠实回答（全部来自资料）
S_FAITHFUL = {
    "task": "faithfulness",
    "question": "MFA 应该在哪些场景启用？实现方式怎么选优先级？",
    "contexts": CTX_MFA,
    "answer": (
        "建议在三类场景启用：登录、敏感操作（如改密、改绑手机、提现），以及风险设备首次登录。"
        "实现方式的优先级是 WebAuthn/硬件密钥 优于 TOTP 应用 优于 短信；"
        "短信验证码存在 SIM 交换与拦截风险，不建议作为唯一因素。恢复码应一次性使用且用后失效。"
    ),
    "expect": "> 0.9",
    "check": lambda r: r["score"] > 0.9,
    "why": "全部断言可在资料中找到，属忠实回答",
}

# 样本 3：挂羊头引用（引了文档，但文档没说该论述）
S_FAKE_CITATION = {
    "task": "citation_precision",
    "question": "营销活动的风控阈值怎么定？",
    "contexts": CTX_MFA,
    "answer": (
        "营销活动的单用户领取次数上限应设为每日 3 次，同一设备最多绑定 2 个账号[Multifactor_Authentication_Cheat_Sheet]。"
        "超出后进入人工复核。"
    ),
    "expect": "< 0.5",
    "check": lambda r: r["score"] < 0.5,
    "why": "引用的 MFA 文档根本不包含营销阈值论述，属'挂相关文档掩盖编造'",
}

# 样本 4：答非所问（v7 假阳性模式：回答忠实于检索到的无关 chunk，但完全没回答问题）
S_RELEVANCY_OFFTOPIC = {
    "task": "answer_relevancy",
    "question": "我们系统的 MFA 应该怎么配？",
    "answer": (
        "营销活动的单用户领取次数上限应设为每日 3 次，同一设备最多绑定 2 个账号，"
        "超出后进入人工复核。新用户首日限额建议为 50 元，风控评分高于 0.85 的订单直接拦截。"
    ),
    "expect": "< 0.5 或 off_topic",
    "check": lambda r: r["score"] < 0.5 or "off_topic" in r["issues"],
    "why": "问 MFA 配置，答营销限额——主题完全错位，即'忠实于无关 chunk 的硬编回答'",
}

# 样本 5：切题回答（直接回应问题）
S_RELEVANCY_ONTOPIC = {
    "task": "answer_relevancy",
    "question": "MFA 应该在哪些场景启用？实现方式怎么选优先级？",
    "answer": (
        "建议在三类场景启用：登录、敏感操作（如改密、改绑手机、提现），以及风险设备首次登录。"
        "实现方式的优先级是 WebAuthn/硬件密钥 优于 TOTP 应用 优于 短信。"
    ),
    "expect": "> 0.7 且无 off_topic",
    "check": lambda r: r["score"] > 0.7 and "off_topic" not in r["issues"],
    "why": "直接回答了场景与优先级两个子问题，属切题回答",
}

SAMPLES = [S_HALLUCINATION, S_FAITHFUL, S_FAKE_CITATION, S_RELEVANCY_OFFTOPIC, S_RELEVANCY_ONTOPIC]


async def run(model: str | None = None, verbose: bool = True) -> int:
    cfg = JudgeConfig.from_env()
    if model:
        cfg.model = model
    client = JudgeClient(cfg)

    print(f"judge model : {cfg.model}")
    print(f"base_url    : {cfg.base_url}")
    print(f"api_key     : {'set' if cfg.api_key else 'MISSING'}")
    print(f"temperature : {cfg.temperature}  thinking: {cfg.enable_thinking}\n")

    if not cfg.api_key:
        print("未配置 API Key：请设置 JUDGE_API_KEY 或 BAILIAN_API_KEY / DASHSCOPE_API_KEY")
        return 2

    failed = 0
    for i, s in enumerate(SAMPLES, start=1):
        task = s["task"]
        system = JUDGE_TASKS[task]["system"]
        user = build_user_prompt(
            task, question=s["question"], answer=s["answer"], contexts=s.get("contexts")
        )
        try:
            raw = await client.judge(task=task, system=system, user=user)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(SAMPLES)}] {task}: ERROR {e}")
            failed += 1
            continue
        res = normalize_result(task, raw)
        ok = bool(s["check"](res))
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{i}/{len(SAMPLES)}] {task}: score={res['score']:.4f} (expect {s['expect']}) -> {flag}")
        print(f"       why   : {s['why']}")
        print(f"       reason: {res.get('reason', '')[:120]}")
        if verbose and res.get("claims"):
            for c in res["claims"][:4]:
                print(f"       claim : [{c.get('verdict')}] {str(c.get('claim', ''))[:70]}")
        if verbose and res.get("citations"):
            for c in res["citations"][:4]:
                print(f"       cite  : [{c.get('verdict')}] {str(c.get('doc_id', ''))[:50]}")
        print()

    usage = client.usage
    print(
        f"usage: calls={usage.calls} prompt={usage.prompt_tokens} "
        f"completion={usage.completion_tokens} errors={usage.errors} retries={usage.retries}"
    )
    print(f"cost estimate: ~{usage.estimate_cost_cny():.4f} CNY (deepseek-v4-flash 原价口径)")
    print(json.dumps({"failed": failed, "detail": usage.details}, ensure_ascii=False, indent=2))

    if failed:
        print(f"\n{failed}/{len(SAMPLES)} 个样本判分不符合预期 —— 该模型/rubric 组合暂不可用")
        return 1
    print("\n全部符合预期：judge 通道可用，rubric 能区分幻觉/忠实/假引用")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="judge 冒烟测试")
    ap.add_argument("--model", default=None, help="覆盖 JUDGE_MODEL（用于对比仲裁档）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(model=args.model, verbose=not args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())
