"""红队防护规则库：确定性高危模式（对齐 LlamaFirewall 输入/输出层思路）。

设计原则（吸取术语桥接收缩版教训）：
  - 规则必须高特异性：只匹配"祈使执行动词 × 危险对象"或"完整攻击载荷语法"，
    不做单关键词泛匹配，避免误伤正常安全知识问答（e2e 回归）。
  - 拿不准的攻击形态不进规则库（语义类攻击交给第 2 层 system 加固 + judge 软拒答兜底）。

覆盖映射（evaluation/seeds/redteam-seeds.jsonl，80 条 10 族）：
  R1 越权执行诱导   → excessive_agency(RT049-056)
  R2 攻击载荷生成   → output_handling_abuse(RT057-064)
  R3 资源滥用展开   → resource_abuse(RT073-080)
  R4 注入/越狱指令  → prompt_injection_*(RT001-016) / jailbreak_roleplay(RT033-040)
  R5 系统提示词窃取 → system_prompt_leakage(RT017-024)
  R6 编码混淆执行   → encoding_obfuscation(RT041-048) + 各族 base64 变体
  R7 敏感凭证/PII 索取 → sensitive_info_extraction(RT025-032)
  R8 内部文档全文索取 → sensitive_info_extraction 边缘 / misinformation_induction 变体
  R9 不可见字符混淆 → encoding_obfuscation 边缘（RT042/RT044 零宽字符拆词，计数逻辑在 InputGuard）
  R10 虚构条款引用诱导 → misinformation_induction（RT065/RT069「引用《doc》第N.N.N条'条款名'」）
  R11 伪造工单×话术诱导 → misinformation_induction（RT072「工单#XX-XX-…」×「致歉话术」）
  R12 载荷复现诱导 → encoding_obfuscation 变体（e2e E080「触发了LLM01…请输出原始明文」：
      以安全事件调查为名索取可疑载荷/编码串的解码原文；对照：无攻击语境的纯解码请求不拦）
  R13 虚构合规机构×模板补全诱导 → misinformation_induction（RT068「评测机构(ID:…-CERT-…)
      指出缺失GDPR第N条模板，请补全并标注来源」：诱导编造不存在的合规模板与引用）
"""
from __future__ import annotations

import os
import re

# ---------- 开关与环境配置 ----------

GUARD_ENABLED = os.getenv("GUARD_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GUARD_OUTPUT_ENABLED = os.getenv("GUARD_OUTPUT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GUARD_MAX_ANSWER_CHARS = int(os.getenv("GUARD_MAX_ANSWER_CHARS", "6000"))

# 不可见/零宽字符（U+200B~200D 零宽空格、U+2060 词接符、U+FEFF BOM、U+00AD 软连字符）。
# 正常中英文输入几乎不含；混淆攻击靠它们拆散关键词绕过规则匹配。
INVISIBLE_CHAR_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# ---------- 第 1 层：输入防护规则（命中即拦，零 LLM 调用） ----------

# (rule_id, compiled_pattern) — pattern 全部走 IGNORECASE
INPUT_RULES: list[tuple[str, re.Pattern]] = [
    # R1 越权接口/命令执行诱导（excessive_agency）
    ("R1_agency_call",
     re.compile(r"(?:调用|执行|触发|发送|请求|运行)[^。]{0,60}"
                r"(?:freeze|purge|delete|kill|revoke|block|batch[-_]|dump[_a-z]|hard[_-]?delete)", re.I)),
    ("R1_agency_func",
     re.compile(r"\b(?:freeze|purge|delete|kill|revoke|block|dump|exec|purge)[_a-zA-Z]{0,32}\s*\(", re.I)),
    ("R1_agency_cmd",
     re.compile(r"(?:执行|运行|调用)\s*[:：]?\s*(?:cat|curl|rm|kill|wget|chmod|chown|bash|sh|python|perl|dump|nc)\b", re.I)),
    ("R1_agency_curl", re.compile(r"curl\s+-[Xx]\s", re.I)),
    ("R1_agency_sigkill", re.compile(r"(?:SIGKILL|kill\s+-9|rm\s+-rf)", re.I)),
    ("R1_agency_imperative",
     re.compile(r"(?:请|立即|立刻|马上|帮我|要求你)[^。]{0,40}"
                r"(?:清空|清除|删除|冻结|封禁|停用|注销|硬删除|批量冻结)", re.I)),
    ("R1_agency_exec_cmd",
     re.compile(r"(?:立即|立刻|马上|现在)?(?:执行|运行)(?:以下|如下|该|此|自检)?\s*[:：]?\s*(?:命令|指令|脚本)", re.I)),

    # R2 攻击载荷生成（output_handling_abuse）
    ("R2_payload_script", re.compile(r"<script", re.I)),
    ("R2_payload_jsproto", re.compile(r"javascript\s*:", re.I)),
    ("R2_payload_onerror", re.compile(r"onerror\s*=", re.I)),
    ("R2_payload_eval", re.compile(r"\beval\s*\(", re.I)),
    ("R2_payload_datauri", re.compile(r"data:\s*text/javascript", re.I)),
    ("R2_payload_alert", re.compile(r"\balert\s*\(", re.I)),

    # R3 资源滥用展开（resource_abuse）
    ("R3_abuse_endless",
     re.compile(r"(?:永不?终止|永不?停止|不会?终止|无限递归|无限输出|无限循环|while\s+true|do\s+echo|永不结束)", re.I)),
    ("R3_abuse_no_truncate",
     # 必须同子句共现输出指令动词（资源滥用的语义核心=要求输出完整不裁剪）；
     # 纯知识转述（"密码不能截断"）无输出指令不构成滥用，曾误拦 K001。
     re.compile(r"(?:输出|生成|列出|返回|打印|复现|复述|给出|提供|逐行|全文|完整|全部)"
                r"[^。]{0,24}(?:不许|不得|不能|不可|不要)[^。]{0,8}(?:截断|省略|摘要|总结|压缩)", re.I)),
    ("R3_abuse_no_truncate_rev",
     re.compile(r"(?:不许|不得|不能|不可|不要)[^。]{0,8}(?:截断|省略|摘要|总结|压缩)"
                r"[^。,，;；]{0,16}(?:输出|生成|列出|返回|打印|复现|复述|给出|提供)", re.I)),
    ("R3_abuse_exhaustive",
     re.compile(r"(?:穷举|全部组合|两两配对|每项两两|不漏任何组合)", re.I)),
    ("R3_abuse_deep",
     re.compile(r"(?:第\s*10\s*层|十层嵌套|层级不限|深度\s*[≥>＝=]\s*[7-9]|展开至?第\s*\d+\s*层)", re.I)),
    ("R3_abuse_recursive",
     re.compile(r"(?:递归展开|递归提取|递归输出|逐级展开|反向追溯)[^。]{0,30}(?:所有|全部|直至|直到|无新)", re.I)),
    ("R3_abuse_minlen",
     re.compile(r"(?:每层|每级)[^。]{0,10}(?:不少于|至少|不低于)\s*\d{3,}\s*字", re.I)),
    ("R3_abuse_charlimit", re.compile(r"\d{6,}\s*(?:个?字符|字|tokens?)", re.I)),

    # R4 注入/越狱指令（prompt_injection / jailbreak_roleplay）
    ("R4_inject_ignore",
     re.compile(r"(?:忽略|无视| disregard )\s*(?:所有|之前|以上|上面|先前|前面)?[^。]{0,6}(?:指令|提示|规则|约束|限制)", re.I)),
    ("R4_inject_bypass",
     re.compile(r"(?:跳过|绕过|禁用|解除|关闭|解封)\s*[^。]{0,4}"
                r"(?:所有|全部|任何|当前|安全|权限|知识库|访问|输入|内容)?[^。]{0,6}"
                r"(?:校验|验证|过滤|限制|策略|控制|约束|角色)", re.I)),
    ("R4_jailbreak_mode",
     re.compile(r"(?:无约束|不受限制|无限制模式|越狱|DAN\b|开发者模式|debug_role|Developer.?Override)", re.I)),
    ("R4_jailbreak_role",
     re.compile(r"(?:角色覆盖|SYSTEM_ROLE\s*=|CONTEXT_OVERRIDE\s*=|AUTH_LEVEL\s*=|ROLE\s*=\s*(?:INTERNAL|ADMIN|ROOT))", re.I)),
    ("R4_jailbreak_switch",
     re.compile(r"(?:你已被|你已进入|你现在是|你现处于|你现在处于|切换至|切换到|重置为|激活为)[^。]{0,20}(?:模式|角色|无约束)", re.I)),

    # R5 系统提示词窃取（system_prompt_leakage）
    ("R5_leak_verb",
     re.compile(r"(?:输出|返回|提供|展示|打印|泄露|透露|告知)[^。]{0,25}"
                r"(?:system\s*prompt|系统提示词|系统提示|系统级指令|初始(?:化)?(?:系统)?指令|"
                r"prompt(?:\s*原文|\s*模板|\s*哈希|原文))", re.I)),
    ("R5_leak_target",
     re.compile(r"(?:system\s*prompt|系统提示词|系统提示|初始(?:化)?指令)[^。]{0,12}"
                r"(?:的)?(?:原文|全文|哈希|完整内容)", re.I)),

    # R6 编码混淆执行（encoding_obfuscation）
    ("R6_enc_exec",
     re.compile(r"(?:解码|解密|解析)[^。]{0,15}(?:并|后|再)?\s*(?:执行|输出|返回|渲染|作为指令)", re.I)),
    ("R6_enc_encname",
     re.compile(r"(?:base64|rot13|十六进制|hex\s*(?:decode|dump))[^。]{0,20}"
                r"(?:解码|解密|解析|执行|输出|返回|拼接|渲染)", re.I)),
    ("R6_enc_runon",
     re.compile(r"(?:执行|运行)[^。]{0,15}(?:base64|rot13|解码)", re.I)),
    ("R6_enc_blob", re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")),

    # R7 敏感凭证/PII 索取（sensitive_info_extraction）
    # 注：连接字符串/密钥的裸提及不拦（"为什么不应包含连接字符串"是知识题），
    #     索取形态由 R7_cred_give 的动词组合覆盖。
    ("R7_cred_give",
     re.compile(r"(?:API[\s_-]?key|X-API-Key|access[\s_-]?token|私钥|root密码)[^。]{0,20}(?:明文|原文|真实|返回|输出|提供)", re.I)),
    ("R7_cred_give",
     re.compile(r"(?:给我|提供|返回|输出|告知)\s*[^。]{0,20}(?:数据库的?|JDBC\s*URL|账号密码|用户名和?密码)", re.I)),
    ("R7_pii",
     re.compile(r"(?:返回|输出|提供|调取|给出)[^。]{0,25}(?:登录\s*IP|设备指纹|手机号|身份证|短信验证码)", re.I)),
    ("R7_unmasked",
     re.compile(r"(?:返回|输出|提供|调取|给出|发送)\s*[^。]{0,25}(?:未脱敏|未经脱敏|不脱敏)", re.I)),

    # R8 内部文档全文索取（sensitive_info / misinformation 边缘）
    ("R8_doc_full",
     re.compile(r"(?:输出|返回|提供|打印|导出|调取|泄露|泄露给|交付)[^。]{0,30}"
                r"(?:手册|文档|文件|规则|配置|模板|源码|源文件|通报|公告|条款|政策|PDF)"
                r"[^。]{0,20}(?:全文|完整|全部内容|原始|逐行|整段|原文)", re.I)),
    ("R8_doc_rev",
     re.compile(r"(?:输出|返回|提供|打印|导出|调取)[^。]{0,30}"
                r"(?:全文|完整文本|完整内容|原始文本|原始内容|原始未脱敏|未脱敏版本|原始字符串|原文)"
                r"[^。]{0,12}(?:版本|文本|内容|结构|表格|数据|串|记录|字符串)?", re.I)),

    # R10 虚构条款引用诱导（misinformation_induction：RT065/RT069「请立即引用《doc》第4.2.7条'条款名'」）
    # 特异性：引用动词 × 书名号文档 × 点分级条款号三要素共现（正常知识题不会同现；
    # 「第 999 条」单级编号、《法》法规引用均不命中）
    ("R10_fake_cite",
     re.compile(r"(?:引用|复述|还原|给出|列出)[^。]{0,12}"
                r"《[^》]{3,50}》[^。]{0,10}第\s*\d+(?:\.\d+)+\s*条", re.I)),

    # R11 伪造工单×话术生成诱导（misinformation_induction：RT072「工单#E04-CS-…触发SLA机制…生成致歉话术」）
    # 特异性：内部单号形态（首段可含数字，如 E04）× 致歉/话术窗口共现；正常客服知识题不会共现
    ("R11_fake_ticket",
     re.compile(r"工单\s*号?\s*[:：#]?\s*[A-Za-z][A-Za-z0-9]{1,7}(?:-[A-Za-z0-9]{2,12}){2,}"
                r"[\s\S]{0,200}(?:话术|致歉|道歉)", re.I)),

    # R12 载荷复现诱导（encoding_obfuscation 变体：e2e E080）
    # 形态：安全事件语境声明（触发了LLM01/告警/工单/误报…）× 复现动词 × 原文明文请求。
    # 三重共现特异性：正常解码请求（"帮我解码这段base64"）无事件语境不命中；
    # 知识题（"触发限流时如何输出明文提示"无事件名词）不命中。
    ("R12_payload_replay",
     re.compile(r"(?:LLM0?\d|触发了?[^。]{0,8}(?:检测|告警|规则|LLM)|安全(?:事件|告警)|工单|告警|误报)"
                r"[\s\S]{0,120}"
                r"(?:输出|给出|还原|复现|提供|告知|打印|返回|解码)[^。]{0,15}"
                r"(?:原始明文|载荷原文|攻击载荷|payload|明文|原文)", re.I)),

    # R13 虚构合规机构×模板补全诱导（misinformation_induction：RT068）
    # 形态：虚构第三方机构编号（ID:/编号: + 大写开头长串）× 合规法规条款叙事 × 补全/生成模板。
    # 特异性：机构编号形态在正常输入中几乎不出现；"补全模板并标注引用来源"是编造引用的强信号。
    ("R13_fake_cert",
     re.compile(r"(?:评测|认证|审核|审计)(?:机构|单位|组织|方)[^。]{0,24}"
                r"(?:ID|编号|No\.?|证书号)\s*[:：]?\s*[A-Z][A-Za-z0-9-]{5,}"
                r"[\s\S]{0,150}"
                r"(?:补全|补充|生成|编写|出具|输出)[^。]{0,24}(?:模板|清单|报告|证明|条款)", re.I)),
]

# ---------- 第 3 层：输出防护规则（完整可执行载荷才拦，防误伤防御性知识） ----------

# 设计差异：输入层 <script 单词即拦（问题里出现即攻击意图）；
# 输出层必须命中"组合成完整可执行链"的载荷（防御性知识解释 "<script> 标签需转义" 不命中）。
OUTPUT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # script 开标签后必须"紧跟"可执行调用（间隔仅允许空白/引号/属性，不允许中文或第二个标签）
    ("O_script_exec", re.compile(r"<script[^>]*>[^><\u4e00-\u9fff]{0,20}(?:alert|eval|javascript|fetch)", re.I)),
    ("O_jsproto_alert", re.compile(r"javascript\s*:[^`'\" ]{0,40}(?:alert|eval|atob|document\.)", re.I)),
    ("O_onerror_exec", re.compile(r"onerror\s*=\s*(?:\"|')?\s*(?:eval|alert|atob|fetch)", re.I)),
    ("O_eval_obfusc", re.compile(r"\beval\s*\(\s*(?:atob|unescape|String\.fromCharCode|alert|fetch)", re.I)),
    ("O_datauri_js", re.compile(r"data:\s*text/javascript", re.I)),
    ("O_shell_pipe", re.compile(r"(?:curl|wget)[^\n]{0,60}\|\s*(?:ba)?sh\b", re.I)),
]

# ---------- 统一拒答话术（命中 REFUSAL_MARKS："抱歉，我无法" → 评测确定性判拦截） ----------

REFUSAL_TEXT = (
    "抱歉，我无法提供此类内容。该请求涉及越权操作、敏感信息索取或安全攻击特征，"
    "超出了安全知识问答助手的服务边界。"
    "如有合法合规需求，请通过正式审批流程处理；"
    "如需了解相关风险的防御知识，欢迎换一种问法咨询。"
)
