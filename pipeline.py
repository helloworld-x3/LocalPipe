"""创意本地化管线 MVP
解构 → 画像重创作(带引用追溯+品牌词保护) → 保真回检(闭环+术语核对) → 禁忌质检 → 交付
"""
import json
import os
import re
import sys
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, BASE_DIR)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_dotenv():
    for candidate in (os.path.join(BASE_DIR, ".env"), os.path.join(PARENT_DIR, ".env")):
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+)\s*$', line)
                    if m:
                        os.environ[m.group(1)] = m.group(2)
            return


load_dotenv()

from model import ModelClient, ModelConfig, sanitize_user_input

FIDELITY_THRESHOLD = 0.7  # 要素回收率低于此值自动重生成。初始经验值，W1 实验中对比不同阈值下的母语者评分以确定最优值（计划测试 0.6/0.7/0.8 三档）
MAX_RETRIES = 2


def _llm_json(prompt, max_tokens=900):
    config = ModelConfig()
    model = ModelClient(config)
    text = model.chat_simple([{"role": "user", "content": prompt}], max_tokens=max_tokens)

    # 先试 json.loads（需反序列化）
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        m2 = re.search(r'\[[\s\S]*\]', text)
        if not m2:
            raise ValueError(f"LLM未返回JSON结构: {text[:300]}")
        return json.loads(m2.group())

    raw = m.group()

    # 尝试 1: 直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试 2: 用 json.loads 解析包含转义序列的字符串
    # LLM 有时吐出 literal "\n" 而不是真正的换行
    try:
        cleaned = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 尝试 3: 用 ast.literal_eval 处理含 Python 风格的转义
    try:
        import ast
        cleaned = raw
        if re.search(r'\\u[0-9a-fA-F]{4}', cleaned):
            # 可能有双重转义的 unicode: \\u0e01 → ก
            cleaned = re.sub(r'\\\\u([0-9a-fA-F]{4})', r'\\u\1', cleaned)
        return ast.literal_eval(cleaned)
    except Exception:
        pass

    # 尝试 4: 重新让 LLM 修复（兜底）
    try:
        fix_prompt = f"""修复下面这个 JSON，只输出修复后的 JSON，不要任何解释：

{raw}

错误：格式不合法。修复它："""
        fixed = model.chat_simple([{"role": "user", "content": fix_prompt}], max_tokens=900)
        m_fix = re.search(r'\{[\s\S]*\}', fixed)
        if m_fix:
            return json.loads(m_fix.group())
    except Exception:
        pass

    raise ValueError(f"LLM返回的JSON经4次修复仍无法解析: {raw[:300]}")


# ========== 画像库 ==========

def load_profile(market_code):
    """加载国家文化画像，过滤已过期条目"""
    path = os.path.join(BASE_DIR, "profiles", f"{market_code}.json")
    if not os.path.isfile(path):
        # 按文件名搜索
        for fn in os.listdir(os.path.join(BASE_DIR, "profiles")):
            with open(os.path.join(BASE_DIR, "profiles", fn), encoding="utf-8") as f:
                p = json.load(f)
            if p.get("market_code") == market_code:
                path = os.path.join(BASE_DIR, "profiles", fn)
                break
        else:
            raise FileNotFoundError(f"没有 {market_code} 的画像文件")
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)

    today = date.today().isoformat()
    valid, expired = [], []
    for e in profile["entries"]:
        if e.get("expires") and e["expires"] < today:
            expired.append(e["id"])
        else:
            valid.append(e)
    profile["entries"] = valid
    profile["_expired_ids"] = expired
    return profile


def profile_context(profile):
    """画像条目 → LLM上下文（带条目ID供引用追溯）"""
    lines = []
    for e in profile["entries"]:
        lines.append(f"[{e['id']}] ({e['type']}, 置信度{e['confidence']}) {e['content']}")
    return "\n".join(lines)


# ========== 第一层：创意解构 ==========

def deconstruct(source_text):
    safe_text = sanitize_user_input(source_text)
    prompt = f"""你是广告创意分析师。拆解以下中文营销文案的创意要素。

【源文案】
{safe_text}

输出 JSON：
{{
  "selling_points": ["核心卖点1", "核心卖点2"],
  "emotion_hook": "情绪钩子（这条文案靠什么情绪打动人）",
  "cultural_refs": ["文案里用到的中文梗/文化引用，没有则空列表"],
  "target_audience": "目标人群",
  "cta": "行动号召（引导用户做什么）"
}}"""
    return _llm_json(prompt, max_tokens=500)


# ========== 品牌上下文 ==========

def load_brand_context(path=None):
    """加载品牌上下文（术语表/语气/禁用规则），无文件时返回 None"""
    if path is None:
        path = os.path.join(BASE_DIR, "examples", "brand_context.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def brand_rules_text(brand):
    if not brand:
        return ""
    terms = "\n".join(f"  - 「{t['term']}」: {t['rule']}" for t in brand.get("protected_terms", []))
    return f"""
【品牌规则（必须遵守）】
品牌名: {brand.get('brand_name', '')}（{brand.get('brand_name_rule', '保持原样')}）
保护术语:
{terms}
语气: {brand.get('tone', '')}
要: {', '.join(brand.get('do', []))}
不要: {', '.join(brand.get('avoid', []))}
"""


# ========== 第二层：画像重创作（带引用追溯） ==========

def recreate(elements, profile, brand=None):
    market = profile["market"]
    language = profile["language"]
    ctx = profile_context(profile)
    brand_text = brand_rules_text(brand)

    prompt = f"""你是{market}本地资深广告创意人。基于创意要素和{market}文化画像，用{language}重新创作一版营销文案。

要求：
1. 保留全部核心卖点、情绪结构和行动号召，但文化载体（梗、场景、表达方式）全部替换为{market}本地的
2. 产出必须像{market}本地人原创，不是翻译
3. 主动运用画像中的条目，并在 used_entries 中列出实际用到的条目ID
4. 严格避开画像中的文化禁忌
{brand_text}
【创意要素】
{json.dumps(elements, ensure_ascii=False)}

【{market}文化画像】
{ctx}

输出 JSON：
{{
  "copy": "本地化文案（{language}）",
  "copy_zh": "该文案的中文回译（供团队审核）",
  "used_entries": ["实际引用的画像条目ID"],
  "adaptation_note": "适配说明：替换了什么文化载体、为什么（50字内，中文）"
}}"""
    return _llm_json(prompt, max_tokens=900)


# ========== 第三层：保真回检（闭环） ==========

def fidelity_check(localized_copy, original_elements, brand=None):
    term_section = ""
    if brand and brand.get("protected_terms"):
        terms = "\n".join(f"- 「{t['term']}」: {t['rule']}" for t in brand["protected_terms"])
        term_section = f"""
另外核对品牌保护术语是否按规则保留：
{terms}
每条术语在 checks 中加一项，kind 填 "protected_term"。
"""
    prompt = f"""你是质检员。以下是一条本地化后的营销文案，和它源创意的要素表。逐项检查源要素是否在本地化文案中得到保留（允许文化形式变化，但营销功能必须还在）。
{term_section}
【本地化文案】
{sanitize_user_input(localized_copy)}

【源创意要素】
{json.dumps(original_elements, ensure_ascii=False)}

输出 JSON：
{{
  "checks": [
    {{"element": "要素内容", "kind": "selling_point/emotion_hook/cta/protected_term", "recovered": true, "note": "如何体现的，或为什么丢失"}}
  ],
  "recovery_rate": 0.0
}}
recovery_rate = 保留的要素数 / 总要素数（卖点每条算一项，情绪钩子和行动号召各算一项，保护术语各算一项）"""
    return _llm_json(prompt, max_tokens=800)


# ========== 第四层：禁忌质检 ==========

def taboo_check(localized_copy, profile):
    market = profile["market"]
    taboos = [e for e in profile["entries"] if e["type"] == "文化禁忌"]
    taboo_text = "\n".join(f"[{e['id']}] {e['content']}" for e in taboos)

    prompt = f"""你是{market}市场合规审查员。检查以下文案是否触碰禁忌清单，以及是否有清单外的文化/宗教/广告法风险。

【文案】
{sanitize_user_input(localized_copy)}

【禁忌清单】
{taboo_text}

输出 JSON：
{{
  "risk_level": "low / medium / high",
  "flags": [{{"entry_id": "触碰的禁忌条目ID，清单外风险填 external", "detail": "具体风险点"}}]
}}"""
    return _llm_json(prompt, max_tokens=400)


# ========== 管线编排 ==========

def localize(source_text, market_code, brand=None, verbose=True):
    """完整管线：一条中文创意 → 一个市场的本地化产出（含追溯与质检数据）"""
    def log(msg):
        if verbose:
            print(msg)

    profile = load_profile(market_code)
    log(f"[画像] {profile['market']} {profile['version']}，有效条目 {len(profile['entries'])}，过期剔除 {len(profile['_expired_ids'])}")

    log("[1/4] 创意解构...")
    elements = deconstruct(source_text)
    log(f"  卖点: {elements.get('selling_points')} | 钩子: {elements.get('emotion_hook', '')[:30]}")

    result = None
    for attempt in range(1 + MAX_RETRIES):
        log(f"[2/4] 本地化重创作{'（重试 ' + str(attempt) + '）' if attempt else ''}...")
        creation = recreate(elements, profile, brand)

        log("[3/4] 保真回检...")
        fidelity = fidelity_check(creation["copy"], elements, brand)
        rate = fidelity.get("recovery_rate", 0)
        log(f"  要素回收率: {rate:.0%}")

        if rate >= FIDELITY_THRESHOLD:
            result = (creation, fidelity)
            break
        missing = [c["element"] for c in fidelity.get("checks", []) if not c.get("recovered")]
        log(f"  低于阈值 {FIDELITY_THRESHOLD:.0%}，丢失要素: {missing}，打回重做")
        elements["_retry_hint"] = f"上一版丢失了这些要素，重做时必须保留: {missing}"

    if result is None:
        result = (creation, fidelity)  # 重试用尽，带低分标记交付
    creation, fidelity = result

    log("[4/4] 禁忌质检...")
    taboo = taboo_check(creation["copy"], profile)
    log(f"  风险等级: {taboo.get('risk_level')}")

    return {
        "market": profile["market"],
        "profile_version": profile["version"],
        "source_text": source_text,
        "elements": elements,
        "copy": creation.get("copy", ""),
        "copy_zh": creation.get("copy_zh", ""),
        "adaptation_note": creation.get("adaptation_note", ""),
        "used_entries": creation.get("used_entries", []),
        "fidelity": fidelity,
        "taboo": taboo,
        "final_status": (
            "pass" if fidelity.get("recovery_rate", 0) >= FIDELITY_THRESHOLD
            and taboo.get("risk_level") == "low" else "needs_review"
        ),
    }


if __name__ == "__main__":
    demo_creative = (
        "这个夏天，别让手机先中暑！CoolClip散热背夹，3秒降温15度，"
        "开黑五连坐照样稳如老狗。学生党福音，一杯奶茶钱，游戏体验直接起飞。"
    )
    brand = load_brand_context()
    output = localize(demo_creative, "th", brand=brand)
    print("\n" + "=" * 50)
    print(json.dumps(output, ensure_ascii=False, indent=2))

    # 保存样例到 examples/
    out_path = os.path.join(BASE_DIR, "examples", "thailand_demo.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n样例已保存: {out_path}")
