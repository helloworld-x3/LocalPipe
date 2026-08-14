"""创意本地化管线 MVP
解构 → 画像重创作(带引用追溯+品牌词保护) → 保真回检(闭环+术语核对) → 禁忌质检 → 交付
"""
import json
import os
import re
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
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

from model import ModelClient, ModelConfig, sanitize_user_input, Cache, Telemetry
from market_code import validate_market_code

FIDELITY_THRESHOLD = float(os.environ.get("FIDELITY_THRESHOLD", "0.7"))
MAX_RETRIES = 2

# 三路线并行执行开关（默认关闭以保持串行确定性；开启后单任务 LLM 耗时约降为 1/3）。
# 三条路线互相独立、无共享可变状态，且全局 RateLimiter 仍限制总请求速率，费用可控。
# 注意：开启后日志输出顺序不再确定，依赖串行顺序的行为测试应在默认（关闭）下运行。
PARALLEL_ROUTES = os.environ.get("LOCALPIPE_PARALLEL_ROUTES", "0") == "1"

# 保真回检要素权重（加权保真率）：品牌保护词与产品事实最重，情绪/CTA 次之。
# 漏一个数字事实比少一个情绪词严重得多——用权重而不是简单平均来衡量。
_KIND_WEIGHTS = {
    "protected_term": 3,  # 品牌保护词：不可丢
    "selling_point": 2,   # 核心卖点
    "product_type": 3,    # 产品形态/类别：防止开衫等产品事实漂移
    "emotion_hook": 1,    # 情绪钩子
    "cta": 1,             # 行动号召
}
_NUMERIC_FACT_WEIGHT = 3  # 含数字的产品事实（如"3秒降温15度"）视为关键要素


def _kind_weight(kind, element=""):
    """要素重要性权重：数字事实/品牌词最重（3），核心卖点次之（2），情绪/CTA 为 1。"""
    if kind == "selling_point" and any(ch.isdigit() for ch in element):
        return _NUMERIC_FACT_WEIGHT
    return _KIND_WEIGHTS.get(kind, 1)
RETRY_BACKOFF = [1, 2, 4]  # _llm_json 退避重试间隔（秒）

_cache = Cache()
_telemetry = Telemetry()


def _make_cache_key(prompt, model, max_tokens, base_url):
    raw = f"{base_url}|{model}|{max_tokens}|{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ========== JSON Schema 校验（响应完整性） ==========

# 各层产出结构定义：(必需键, 类型约束)
_SCHEMAS = {
    "deconstruct": {
        "required": ["selling_points", "emotion_hook", "target_audience", "cta"],
        "types": {
            "selling_points": list, "emotion_hook": str, "target_audience": str, "cta": str,
            "product_type": str,
        },
    },
    "recreate": {
        "required": ["copy", "copy_zh", "used_entries", "adaptation_note"],
        "types": {"copy": str, "copy_zh": str, "used_entries": list, "adaptation_note": str},
    },
    "fidelity": {
        "required": ["checks", "recovery_rate"],
        "types": {"checks": list, "recovery_rate": (int, float)},
    },
    "taboo": {
        "required": ["risk_level", "flags"],
        "types": {"risk_level": str, "flags": list},
    },
    "reviews_ai": {
        "required": ["problem_categories", "feedback_summary", "revision_candidates"],
        "types": {"problem_categories": list, "feedback_summary": str, "revision_candidates": list},
    },
}


def validate_schema(data, layer_name):
    """校验 LLM 响应结构完整性——防响应篡改/模型输出畸变"""
    schema = _SCHEMAS.get(layer_name)
    if not schema:
        return  # 未注册的层跳过

    missing = [k for k in schema["required"] if k not in data]
    if missing:
        raise ValueError(f"[{layer_name}] Schema 校验失败：缺少字段 {missing}。响应: {json.dumps(data, ensure_ascii=False)[:200]}")

    for key, expected in schema["types"].items():
        if key in data and not isinstance(data[key], expected):
            raise ValueError(f"[{layer_name}] Schema 校验失败：{key} 类型应为 {expected}, 实际 {type(data[key])}")


# ========== 画像完整性校验 ==========

def _profile_hash_path():
    return os.path.join(BASE_DIR, "profiles", ".hashes.json")


def _load_hashes():
    p = _profile_hash_path()
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def verify_profile_integrity(path, market_code):
    """SHA256 版本完整性校验——检测画像文件意外修改"""
    hashes = _load_hashes()
    if market_code not in hashes:
        return  # 无基线哈希，跳过（首次使用需先 gen_profile_hashes）

    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()

    expected = hashes[market_code]
    if actual != expected:
        raise RuntimeError(
            f"画像完整性校验失败: {market_code}.json 的 SHA256 不匹配！"
            f"预期 {expected[:16]}...，实际 {actual[:16]}..."
            f"如确认为有意修改，请执行 gen_profile_hashes() 更新基线"
        )


def gen_profile_hashes():
    """生成/更新所有画像文件的 SHA256 基线（在已知安全环境下执行）"""
    profile_dir = os.path.join(BASE_DIR, "profiles")
    hashes = {}
    for fn in sorted(os.listdir(profile_dir)):
        if fn.endswith(".json") and not fn.startswith("."):
            with open(os.path.join(profile_dir, fn), "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            # 从文件内容提取 market_code
            with open(os.path.join(profile_dir, fn), encoding="utf-8") as f:
                code = json.load(f).get("market_code", fn.replace(".json", ""))
            hashes[code] = h
            print(f"  {code}: {h[:16]}...")

    with open(_profile_hash_path(), "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2)
    print(f"哈希基线已保存: {_profile_hash_path()}")
    return hashes


def _parse_json_text(text, schema, model=None):
    """4 级 JSON 解析：直接解析 → 转义修复 → ast.literal_eval → LLM 修复"""

    def _parse_and_validate(raw):
        data = json.loads(raw)
        if schema:
            validate_schema(data, schema)
        return data

    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        m2 = re.search(r'\[[\s\S]*\]', text)
        if not m2:
            raise ValueError(f"LLM未返回JSON结构: {text[:300]}")
        result = json.loads(m2.group())
        if schema:
            validate_schema(result, schema)
        return result

    raw = m.group()

    # 尝试 1: 直接解析
    try:
        return _parse_and_validate(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试 2: literal "\n" → 真正的换行
    try:
        cleaned = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return _parse_and_validate(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试 3: ast.literal_eval 处理 Python 风格转义
    try:
        import ast
        cleaned = raw
        if re.search(r'\\u[0-9a-fA-F]{4}', cleaned):
            cleaned = re.sub(r'\\\\u([0-9a-fA-F]{4})', r'\\u\1', cleaned)
        data = ast.literal_eval(cleaned)
        if schema:
            validate_schema(data, schema)
        return data
    except Exception:
        pass

    # 尝试 4: 让 LLM 修复（兜底）
    if model:
        try:
            fix_prompt = f"""修复下面这个 JSON，只输出修复后的 JSON，不要任何解释：

{raw}

错误：格式不合法。修复它："""
            fixed = model.chat_simple([{"role": "user", "content": fix_prompt}], max_tokens=900)
            m_fix = re.search(r'\{[\s\S]*\}', fixed)
            if m_fix:
                return _parse_and_validate(m_fix.group())
        except Exception:
            pass

    raise ValueError(f"LLM返回的JSON经4次修复仍无法解析: {raw[:300]}")


def _llm_json(prompt, max_tokens=900, schema=None):
    config = ModelConfig()
    model = ModelClient(config)

    cache_key = _make_cache_key(prompt, config.model, max_tokens, config.base_url)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            text = model.chat_simple([{"role": "user", "content": prompt}], max_tokens=max_tokens, response_format={"type": "json_object"})
            result = _parse_json_text(text, schema, model=model)
            _cache.set(cache_key, result)
            return result
        except Exception as e:
            last_error = e
            if attempt < len(RETRY_BACKOFF):
                wait = RETRY_BACKOFF[attempt]
                print(f"  [_llm_json 重试 {attempt + 1}/{len(RETRY_BACKOFF)}，{wait}s 后] {e}")
                time.sleep(wait)

    raise last_error


# ========== 画像库 ==========

def load_profile(market_code):
    """加载国家文化画像，过滤已过期条目"""
    market_code = validate_market_code(market_code)
    path = os.path.join(BASE_DIR, "profiles", f"{market_code}.json")
    if not os.path.isfile(path):
        # 按 market_code 字段搜索（跳过 history 目录与非 JSON 文件）
        for fn in os.listdir(os.path.join(BASE_DIR, "profiles")):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            try:
                with open(os.path.join(BASE_DIR, "profiles", fn), encoding="utf-8") as f:
                    p = json.load(f)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if p.get("market_code") == market_code:
                path = os.path.join(BASE_DIR, "profiles", fn)
                break
        else:
            raise FileNotFoundError(f"没有 {market_code} 的画像文件")
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)

    verify_profile_integrity(path, market_code)

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
    # 文化维度只作非条目背景，不能伪装成可追溯的画像 ID。
    # 否则模型容易把 "hofstede" 写入 used_entries，随后被证据门正确判为无效引用。
    dims = profile.get("cultural_dimensions", {})
    dim_values = dims.get("dimensions") or {}
    if dim_values:
        dim_text = "、".join(f"{k}={v}" for k, v in dim_values.items())
        usage = str(dims.get("usage", "不得单独推出广告结论")).strip()
        lines.append(
            "【文化维度背景（非画像条目，不得写入 used_entries）】"
            f"{dim_text}；{usage}。只能用上方带正式 ID 的画像条目支撑创作结论。"
        )

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
  "cta": "行动号召（引导用户做什么）",
  "product_type": "产品形态/类别（如针织开衫、连衣裙；无法确认则空字符串）"
}}"""
    return _llm_json(prompt, max_tokens=500, schema="deconstruct")


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

    def _san(v):
        v = str(v or "").strip()
        return sanitize_user_input(v) if v else ""

    terms = "\n".join(
        f"  - 「{_san(t.get('term', ''))}」: {_san(t.get('rule', ''))}"
        for t in brand.get("protected_terms", [])
        if isinstance(t, dict)
    )
    brand_name = _san(brand.get("brand_name"))
    return f"""
【品牌规则（必须遵守）】
品牌名: {brand_name}（{_san(brand.get('brand_name_rule', '保持原样'))}）
保护术语:
{terms}
语气: {_san(brand.get('tone'))}
要: {', '.join(_san(x) for x in brand.get('do', []))}
不要: {', '.join(_san(x) for x in brand.get('avoid', []))}
"""


# ========== 第二层：画像重创作（带引用追溯） ==========

def _sanitize_elements(value):
    """递归清洗进入 prompt 的要素文本（防 LLM 解构产物携带注入指令）。

    结构隔离为主：字符串经 sanitize_user_input 包裹进 <user_input> 标签并做
    HTML 实体转义；dict/list 递归处理，非字符串原样保留。仅用于 prompt 文本，
    规则计算（_build_expected_checks 等）仍使用原始值。
    """
    if isinstance(value, dict):
        return {key: _sanitize_elements(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_elements(item) for item in value]
    if isinstance(value, str):
        return sanitize_user_input(value)
    return value


def _build_creative_routes(elements, profile):
    """Build stable route contracts from source elements and citable profile entries."""
    positive_entries = [
        entry for entry in profile.get("entries", [])
        if isinstance(entry, dict) and entry.get("type") != "文化禁忌" and entry.get("id")
    ]
    evidence_ids = [entry["id"] for entry in positive_entries]
    selling_points = elements.get("selling_points", []) if isinstance(elements, dict) else []
    primary_selling_point = selling_points[0] if selling_points else ""
    emotion_hook = elements.get("emotion_hook", "") if isinstance(elements, dict) else ""

    shared = {
        "evidence_ids": evidence_ids,
        "constraints": [
            "保留全部源卖点、CTA 和品牌保护词",
            "不得新增源 Brief 或画像条目无法支持的产品事实",
            "只能引用本路线 evidence_ids 中的非禁忌画像条目",
        ],
    }
    return [
        {
            **shared,
            "route_id": "product_proof",
            "objective": "优先用具体、可核验的产品证据组织文案，再承接情绪表达",
            "focus": primary_selling_point,
        },
        {
            **shared,
            "route_id": "scene_fit",
            "objective": "使用画像支持的真实使用场景组织卖点，不虚构市场事实",
            "focus": emotion_hook,
        },
        {
            **shared,
            "route_id": "brand_emotion",
            "objective": "保留产品事实，以源情绪钩子和品牌语气重构开场",
            "focus": emotion_hook,
        },
    ]


def _creative_route_text(route):
    if not isinstance(route, dict):
        return ""
    return (
        "\n【本轮创意路线】\n"
        f"{json.dumps(route, ensure_ascii=False)}\n"
        "路线只决定表达重心，不得覆盖保真、证据引用和禁忌规则。\n"
    )


def recreate(elements, profile, brand=None):
    market = profile["market"]
    language = profile["language"]
    ctx = profile_context(profile)
    brand_text = brand_rules_text(brand)
    route = elements.get("_creative_route") if isinstance(elements, dict) else None
    source_elements = {
        key: value for key, value in elements.items() if key != "_creative_route"
    }
    route_text = _creative_route_text(route)

    prompt = f"""你是{market}本地资深广告创意人。基于创意要素和{market}文化画像，用{language}重新创作一版营销文案。

严格只输出下方 JSON 结构，不要前言、不要后记、不要解释：

要求：
1. 保留全部核心卖点、情绪结构和行动号召，但文化载体（梗、场景、表达方式）全部替换为{market}本地的
2. 产出必须像{market}本地人原创，不是翻译
3. 主动运用画像中的条目，并在 used_entries 中列出实际用到的条目ID。注意：只能引用 type 非"文化禁忌"的条目，禁忌条目应当规避而非引用
4. 严格避开画像中的文化禁忌
{brand_text}
{route_text}
【创意要素】
{json.dumps(_sanitize_elements(source_elements), ensure_ascii=False)}

【{market}文化画像】
{ctx}

输出 JSON：
{{
  "copy": "本地化文案（{language}）",
  "copy_zh": "该文案的中文回译（供团队审核）",
  "used_entries": ["实际引用的画像条目ID"],
  "adaptation_note": "适配说明：替换了什么文化载体、为什么（50字内，中文）"
}}"""
    return _llm_json(prompt, max_tokens=900, schema="recreate")

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

【文化对齐要求】（2026-07-30 增强，依据 Hofstede CAT 文化对齐评估思路）
除要素保真外，额外核对：本地化文案是否符合目标市场的文化风格（语言含蓄度/直白度、幽默方式、权威叙事 vs 平权叙事、价格敏感表达）。
如发现文化风格与目标市场明显不符（例如对高不确定性规避市场使用夸张绝对化承诺、对低语境市场使用高语境含蓄暗示），
在 checks 中追加一项 kind=cultural_alignment、recovered=false 的检查，并注明是文化对齐问题而非要素丢失。
{term_section}
【本地化文案】
{sanitize_user_input(localized_copy)}

【源创意要素】
{json.dumps(_sanitize_elements(original_elements), ensure_ascii=False)}

输出 JSON：
{{
  "checks": [
    {{"element": "要素内容", "kind": "selling_point/emotion_hook/cta/product_type/protected_term", "recovered": true, "note": "如何体现的，或为什么丢失"}}
  ],
  "recovery_rate": 0.0
}}
recovery_rate = 保留的要素数 / 总要素数（卖点每条算一项，情绪钩子和行动号召各算一项，保护术语各算一项）"""
    return _llm_json(prompt, max_tokens=800, schema="fidelity")


# ========== 第四层：禁忌质检 ==========

def taboo_check(localized_copy, profile, source_text=None):
    market = profile["market"]
    taboos = [e for e in profile["entries"] if e["type"] == "文化禁忌"]
    taboo_text = "\n".join(f"[{e['id']}] {e['content']}" for e in taboos)
    source_section = ""
    if source_text:
        source_section = f"""
【源文案（中文）—— 必须一并审查】
{sanitize_user_input(source_text)}
若源文案存在违反禁忌清单的诉求（功效夸大、身材/外貌承诺、绝对化用语等），
即使本地化文案已规避，也必须判源文案违规，风险等级升为 medium 或 high，
flags 中注明对应禁忌条目 ID（清单外风险填 external）。
"""

    prompt = f"""你是{market}市场合规审查员。检查以下文案是否触碰禁忌清单，以及是否有清单外的文化/宗教/广告法风险。
{source_section}
【本地化文案】
{sanitize_user_input(localized_copy)}

【禁忌清单】
{taboo_text}

输出 JSON：
{{
  "risk_level": "low / medium / high",
  "flags": [{{"entry_id": "触碰的禁忌条目ID，清单外风险填 external", "detail": "具体风险点"}}]
}}"""
    return _llm_json(prompt, max_tokens=400, schema="taboo")


# ========== 管线编排 ==========

def compute_recovery_rate(checks):
    """纯函数：简单平均口径的真实恢复率（legacy/测试用）。

    主管线用 _evaluate_fidelity_checks 的加权保真率（品牌词/数字事实权重更高）。
    - checks 为空或非 list → 0.0
    - 单个 check 缺少 recovered 字段 → 按 False
    - recovered 必须严格为 True（布尔），字符串 "false" 等真值无效
    - check 非 dict → 跳过不计入总数和回收数
    """
    if not checks or not isinstance(checks, list):
        return 0.0
    valid = [c for c in checks if isinstance(c, dict)]
    if not valid:
        return 0.0
    recovered = sum(1 for c in valid if c.get("recovered") is True)
    return recovered / len(valid)


def _build_expected_checks(elements, brand=None):
    """从源要素构造 (kind, element) 预期项，保留跨类型同文案。"""
    expected = []
    seen = set()
    for sp in elements.get("selling_points", []):
        if not isinstance(sp, str):
            raise ValueError("selling_points 中的要素必须是字符串")
        item = ("selling_point", sp)
        if sp and item not in seen:
            expected.append(item)
            seen.add(item)
    for kind in ("emotion_hook", "cta"):
        element = elements.get(kind, "")
        item = (kind, element)
        if element and item not in seen:
            expected.append(item)
            seen.add(item)
    product_type = elements.get("product_type", "")
    if isinstance(product_type, str) and product_type:
        item = ("product_type", product_type)
        if item not in seen:
            expected.append(item)
            seen.add(item)
    if brand and brand.get("protected_terms"):
        for t in brand["protected_terms"]:
            term = t.get("term", "") if isinstance(t, dict) else ""
            item = ("protected_term", term)
            if term and item not in seen:
                expected.append(item)
                seen.add(item)
    return expected


def _evaluate_fidelity_checks(expected, checks):
    """按 (kind, element) 严格核对 checks，重复或非布尔结果均不通过。

    文化对齐是独立门：模型在 fidelity 阶段发现文案风格与目标市场文化维度
    明显不符时，会追加 kind=cultural_alignment 的检查。该检查不参与要素
    回收率计算，但 recovered 非 True 时整体判定文化对齐失败（打回重做）。
    """
    check_map = {}
    unexpected = []
    expected_set = set(expected)
    alignment_checked = False
    alignment_failed = False
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        kind = check.get("kind")
        element = check.get("element")
        if not isinstance(kind, str) or not isinstance(element, str):
            unexpected.append({
                "kind": kind,
                "element": element,
                "reason": "invalid_key_type",
            })
            continue
        if kind == "cultural_alignment":
            alignment_checked = True
            if check.get("recovered") is not True:
                alignment_failed = True
                unexpected.append({
                    "kind": kind,
                    "element": element,
                    "reason": "cultural_alignment_failed",
                })
            continue
        key = (kind, element)
        if key not in expected_set:
            unexpected.append({
                "kind": kind,
                "element": element,
                "reason": "unexpected",
            })
            continue
        check_map.setdefault(key, []).append(check)

    matched = []
    failed = []
    for key in expected:
        candidates = check_map.get(key, [])
        if not candidates:
            reason = "missing"
        elif len(candidates) > 1:
            reason = "duplicate"
        elif candidates[0].get("recovered") is True:
            matched.append(key)
            continue
        elif isinstance(candidates[0].get("recovered"), bool):
            reason = "not_recovered"
        else:
            reason = "recovered_not_bool"
        failed.append({"kind": key[0], "element": key[1], "reason": reason})

    rate_unweighted = len(matched) / len(expected) if expected else 0.0
    weight_matched = sum(_kind_weight(kind, element) for kind, element in matched)
    weight_total = sum(_kind_weight(kind, element) for kind, element in expected)
    rate = weight_matched / weight_total if weight_total else 0.0
    structure_valid = bool(expected) and all(
        item["reason"] == "not_recovered" for item in failed
    )
    return {
        "rate": rate,
        "rate_unweighted": rate_unweighted,
        "structure_valid": structure_valid,
        "alignment_checked": alignment_checked,
        "alignment_failed": alignment_failed,
        "matched": [{"kind": kind, "element": element} for kind, element in matched],
        "failed": failed,
        "unexpected": unexpected,
    }


def _trace_profile_entries(creation, profile):
    all_entry_ids = {entry["id"] for entry in profile["entries"]}
    taboo_ids = {
        entry["id"] for entry in profile["entries"] if entry["type"] == "文化禁忌"
    }
    raw_used = creation.get("used_entries", [])
    used = list(dict.fromkeys(raw_used)) if isinstance(raw_used, list) else []
    creation["used_entries"] = used
    trace = {
        "valid_ids": [uid for uid in used if uid in all_entry_ids and uid not in taboo_ids],
        "invalid_ids": [uid for uid in used if uid not in all_entry_ids],
        "taboo_ids": [uid for uid in used if uid in taboo_ids],
        "empty_reference": len(used) == 0,
    }
    creation["profile_trace"] = trace
    return trace


def _candidate_status(creation, fidelity, taboo, threshold):
    if not creation:
        return "error"
    if not fidelity:
        return "needs_review"
    trace = creation.get("profile_trace", {})
    trace_clean = (
        not trace.get("invalid_ids")
        and not trace.get("taboo_ids")
        and not trace.get("empty_reference")
    )
    passed = (
        fidelity.get("recovery_rate", 0.0) >= threshold
        and fidelity.get("structure_valid") is True
        and fidelity.get("_alignment_failed") is not True
        and (taboo or {}).get("risk_level") == "low"
        and trace_clean
    )
    return "pass" if passed else "needs_review"


def _evaluate_route_candidate(source_text, elements, profile, brand, route, log):
    """Run layers 2-4 for one route without mutating the shared decomposition."""
    route_id = route["route_id"]
    routed_elements = dict(elements)
    routed_elements["_creative_route"] = route
    creation = None
    fidelity = None
    taboo = None
    errors = []
    timings = {}
    fidelity_retries = 0

    try:
        for attempt in range(1 + MAX_RETRIES):
            if attempt:
                fidelity_retries += 1
            log(
                f"[2/4:{route_id}] 本地化重创作"
                f"{'（重试 ' + str(attempt) + '）' if attempt else ''}..."
            )
            started = time.time()
            creation = recreate(routed_elements, profile, brand)
            timings["recreate_ms"] = timings.get("recreate_ms", 0) + round(
                (time.time() - started) * 1000
            )
            trace = _trace_profile_entries(creation, profile)
            if trace["invalid_ids"]:
                log(f"  [{route_id}] used_entries 含无效ID: {trace['invalid_ids']}")
            if trace["taboo_ids"]:
                log(f"  [{route_id}] used_entries 含禁忌条目: {trace['taboo_ids']}")

            log(f"[3/4:{route_id}] 保真回检...")
            started = time.time()
            fidelity = fidelity_check(creation["copy"], elements, brand)
            timings["fidelity_ms"] = timings.get("fidelity_ms", 0) + round(
                (time.time() - started) * 1000
            )
            expected_checks = _build_expected_checks(elements, brand)
            evaluation = _evaluate_fidelity_checks(
                expected_checks, fidelity.get("checks", [])
            )
            fidelity["recovery_rate"] = evaluation["rate"]
            fidelity["recovery_rate_unweighted"] = evaluation["rate_unweighted"]
            fidelity["structure_valid"] = evaluation["structure_valid"]
            fidelity["_structure_valid"] = evaluation["structure_valid"]
            fidelity["_alignment_checked"] = evaluation["alignment_checked"]
            fidelity["_alignment_failed"] = evaluation["alignment_failed"]
            fidelity["_expected"] = [
                {"kind": kind, "element": element} for kind, element in expected_checks
            ]
            fidelity["_matched"] = evaluation["matched"]
            fidelity["_failed"] = evaluation["failed"]
            if evaluation["unexpected"]:
                fidelity["_unexpected"] = evaluation["unexpected"]

            if (
                evaluation["rate"] >= FIDELITY_THRESHOLD
                and evaluation["structure_valid"]
                and not evaluation["alignment_failed"]
            ):
                break
            failed_summary = [
                f"{item['kind']}:{item['element']}({item['reason']})"
                for item in evaluation["failed"]
            ]
            if evaluation["alignment_failed"]:
                failed_summary.append("cultural_alignment: 文化表达未对齐")
            routed_elements["_retry_hint"] = (
                "上一版这些要素未通过保真检查，重做时必须保留: "
                f"{failed_summary}"
            )
    except Exception as exc:
        errors.append(f"recreate/fidelity: {exc}")
        log(f"  [{route_id}] 重创作/回检失败: {exc}")

    if creation:
        try:
            log(f"[4/4:{route_id}] 禁忌质检...")
            started = time.time()
            taboo = taboo_check(creation["copy"], profile, source_text=source_text)
            timings["taboo_ms"] = round((time.time() - started) * 1000)
        except Exception as exc:
            errors.append(f"taboo: {exc}")
            taboo = {"risk_level": "unknown", "flags": [], "_error": str(exc)}
            log(f"  [{route_id}] 禁忌质检失败: {exc}")

    final_status = _candidate_status(
        creation, fidelity, taboo, FIDELITY_THRESHOLD
    )
    return {
        "route_id": route_id,
        "creative_route": route,
        "available_evidence_ids": list(route.get("evidence_ids", [])),
        "copy": creation.get("copy", "") if creation else "",
        "copy_zh": creation.get("copy_zh", "") if creation else "",
        "adaptation_note": creation.get("adaptation_note", "") if creation else "",
        "used_entries": creation.get("used_entries", []) if creation else [],
        "profile_trace": creation.get("profile_trace", {}) if creation else {},
        "fidelity": fidelity,
        "taboo": taboo,
        "final_status": final_status,
        "fidelity_retries": fidelity_retries,
        "timings": timings,
        "errors": errors if errors else None,
        "error": "; ".join(errors) if errors else None,
    }


def _localize_competitive(source_text, market_code, profile, elements, brand, log, t_start, timings):
    from candidate_selection import build_selection_decision

    routes = _build_creative_routes(elements, profile)
    if PARALLEL_ROUTES and len(routes) > 1:
        # 三路线互相独立（_evaluate_route_candidate 只读共享输入，内部使用 dict 副本），
        # 并行执行后 executor.map 仍按 routes 顺序返回候选，选择逻辑不受影响。
        with ThreadPoolExecutor(max_workers=len(routes)) as executor:
            candidates = list(executor.map(
                lambda route: _evaluate_route_candidate(
                    source_text, elements, profile, brand, route, log
                ),
                routes,
            ))
    else:
        candidates = [
            _evaluate_route_candidate(source_text, elements, profile, brand, route, log)
            for route in routes
        ]
    decision = build_selection_decision(candidates, FIDELITY_THRESHOLD)
    winner = decision["selected"]
    # Keep the highest-ranked blocked candidate as a diagnostic payload for
    # legacy top-level fields, while ``selected`` remains None and no route is
    # exposed as a publishable recommendation.
    diagnostic_winner = winner or (decision["ranked"][0] if decision["ranked"] else {})
    timings["total_ms"] = round((time.time() - t_start) * 1000)

    if not decision["selected"] and decision["review_policy"] == "block":
        final_status = "needs_review" if diagnostic_winner.get("copy") else "error"
    else:
        final_status = diagnostic_winner.get("final_status", "needs_review")

    rankings = [
        {
            "rank": candidate["rank"],
            "route_id": candidate.get("route_id"),
            "score": candidate["score"],
            "eligible": candidate["eligible"],
            "hard_gate_reasons": candidate["hard_gate_reasons"],
            "components": candidate["components"],
        }
        for candidate in decision["ranked"]
    ]
    selection_trace = {
        "mode": "competitive",
        "selected_route_id": winner.get("route_id", "") if winner else "",
        "weights": winner.get("weights", {}) if winner else {},
        "score_margin": decision["uncertainty"]["margin"],
        "rankings": rankings,
    }
    candidate_errors = []
    fidelity_retries = 0
    for candidate in candidates:
        fidelity_retries += int(candidate.get("fidelity_retries") or 0)
        candidate_errors.extend(candidate.get("errors") or [])
    _telemetry.log({
        "event": "localize",
        "market": market_code,
        "selection_mode": "competitive",
        "selected_route_id": winner.get("route_id", "") if winner else "",
        "review_policy": decision["review_policy"],
        "final_status": final_status,
        "timings": timings,
        "fidelity_retries": fidelity_retries,
        "errors": candidate_errors,
    })
    return {
        "market": profile["market"],
        "profile_version": profile["version"],
        "source_text": source_text,
        # Keep the shared decomposition for Feishu diagnostics even when no
        # candidate is publishable; candidate-level errors remain in
        # ``candidates`` and the top-level copy stays empty.
        "elements": elements if (diagnostic_winner.get("copy") or candidates) else None,
        # A blocked run has no publishable top-level copy. The diagnostic
        # candidate remains available under ``candidates`` for Feishu review.
        "copy": winner.get("copy", "") if winner else "",
        "copy_zh": winner.get("copy_zh", "") if winner else "",
        "adaptation_note": winner.get("adaptation_note", "") if winner else "",
        "used_entries": diagnostic_winner.get("used_entries", []),
        "profile_trace": diagnostic_winner.get("profile_trace", {}),
        "fidelity": diagnostic_winner.get("fidelity"),
        "taboo": diagnostic_winner.get("taboo"),
        "final_status": final_status,
        "errors": diagnostic_winner.get("errors"),
        "candidates": decision["ranked"],
        "selection_trace": selection_trace,
        "uncertainty": decision["uncertainty"],
        "review_policy": decision["review_policy"],
    }


def localize(source_text, market_code, brand=None, verbose=True):
    """完整管线：一条中文创意 → 一个市场的本地化产出（含追溯与质检数据）
    单层失败不崩全链路，返回部分结果 + error 字段。"""
    def log(msg):
        if verbose:
            print(msg)

    t_start = time.time()
    timings = {}
    fidelity_retries = 0
    errors = []

    # [0] 加载画像（画像失败无法继续）
    try:
        profile = load_profile(market_code)
        log(f"[画像] {profile['market']} {profile['version']}，有效条目 {len(profile['entries'])}，过期剔除 {len(profile['_expired_ids'])}")
    except Exception as e:
        log(f"[画像] 加载失败: {e}")
        _telemetry.log({"event": "localize", "market": market_code, "error": f"profile_load: {e}"})
        return {
            "market": market_code,
            "source_text": source_text,
            "error": f"画像加载失败: {e}",
            "final_status": "error",
        }

    # [1] 创意解构
    log("[1/4] 创意解构...")
    t1 = time.time()
    try:
        elements = deconstruct(source_text)
        timings["deconstruct_ms"] = round((time.time() - t1) * 1000)
        log(f"  卖点: {elements.get('selling_points')} | 钩子: {elements.get('emotion_hook', '')[:30]}")
    except Exception as e:
        log(f"  解构失败: {e}")
        timings.update({"deconstruct_ms": round((time.time() - t1) * 1000), "total_ms": round((time.time() - t_start) * 1000)})
        _telemetry.log({"event": "localize", "market": market_code, "error": f"deconstruct: {e}", "timings": timings})
        return {
            "market": profile["market"],
            "source_text": source_text,
            "error": f"创意解构失败: {e}",
            "final_status": "error",
        }

    if os.environ.get("LOCALPIPE_SELECTION_MODE", "competitive").strip().lower() != "legacy":
        return _localize_competitive(
            source_text, market_code, profile, elements, brand, log, t_start, timings
        )

    # [2+3] 重创作 + 保真回检（带 fidelity 打回循环）
    creation = None
    fidelity = None
    verified_recovery_rate = None
    fidelity_structure_valid = False
    alignment_failed = False
    try:
        for attempt in range(1 + MAX_RETRIES):
            if attempt > 0:
                fidelity_retries += 1
            log(f"[2/4] 本地化重创作{'（重试 ' + str(attempt) + '）' if attempt else ''}...")
            t2 = time.time()
            creation = recreate(elements, profile, brand)
            timings["recreate_ms"] = round((time.time() - t2) * 1000)

            # 校验 used_entries：去重 + 真实性 + 非禁忌
            all_entry_ids = {e["id"] for e in profile["entries"]}
            taboo_ids = {e["id"] for e in profile["entries"] if e["type"] == "文化禁忌"}
            used = list(dict.fromkeys(creation.get("used_entries", [])))  # 去重保序
            creation["used_entries"] = used  # 写回去重后列表
            profile_trace = {
                "valid_ids": [uid for uid in used if uid in all_entry_ids and uid not in taboo_ids],
                "invalid_ids": [uid for uid in used if uid not in all_entry_ids],
                "taboo_ids": [uid for uid in used if uid in taboo_ids],
                "empty_reference": len(used) == 0,
            }
            creation["profile_trace"] = profile_trace
            if profile_trace["invalid_ids"]:
                log(f"  ⚠ used_entries 含无效ID: {profile_trace['invalid_ids']}")
            if profile_trace["taboo_ids"]:
                log(f"  ⚠ used_entries 含禁忌条目: {profile_trace['taboo_ids']}（应为规避而非引用）")

            log("[3/4] 保真回检...")
            t3 = time.time()
            fidelity = fidelity_check(creation["copy"], elements, brand)
            timings["fidelity_ms"] = round((time.time() - t3) * 1000)

            expected_checks = _build_expected_checks(elements, brand)
            evaluation = _evaluate_fidelity_checks(
                expected_checks, fidelity.get("checks", [])
            )
            rate = evaluation["rate"]
            verified_recovery_rate = rate
            fidelity_structure_valid = evaluation["structure_valid"]
            alignment_failed = bool(evaluation["alignment_failed"])
            fidelity["recovery_rate"] = rate
            fidelity["recovery_rate_unweighted"] = evaluation["rate_unweighted"]
            fidelity["_structure_valid"] = fidelity_structure_valid
            fidelity["_alignment_checked"] = evaluation["alignment_checked"]
            fidelity["_alignment_failed"] = alignment_failed
            fidelity["_expected"] = [
                {"kind": kind, "element": element}
                for kind, element in expected_checks
            ]
            fidelity["_matched"] = evaluation["matched"]
            fidelity["_failed"] = evaluation["failed"]
            if evaluation["unexpected"]:
                fidelity["_unexpected"] = evaluation["unexpected"]
            log(
                f"  要素回收率: {rate:.0%} "
                f"({len(evaluation['matched'])}/{len(expected_checks)} 匹配, 程序重算)"
            )

            if rate >= FIDELITY_THRESHOLD and fidelity_structure_valid and not alignment_failed:
                break
            failed_summary = [
                f"{item['kind']}:{item['element']}({item['reason']})"
                for item in evaluation["failed"]
            ]
            if alignment_failed:
                failed_summary.append(
                    "cultural_alignment: 文案风格与目标市场文化维度明显不符，需重新对齐文化表达"
                )
            log(f"  低于阈值 {FIDELITY_THRESHOLD:.0%}，未通过要素: {failed_summary}，打回重做")
            elements["_retry_hint"] = (
                "上一版这些要素未通过保真检查，重做时必须保留: "
                f"{failed_summary}"
            )
    except Exception as e:
        log(f"  重创作/回检失败: {e}")
        errors.append(f"recreate/fidelity: {e}")

    # [4] 禁忌质检
    taboo = None
    if creation:
        try:
            log("[4/4] 禁忌质检...")
            t4 = time.time()
            taboo = taboo_check(creation["copy"], profile, source_text=source_text)
            timings["taboo_ms"] = round((time.time() - t4) * 1000)
            log(f"  风险等级: {taboo.get('risk_level')}")
        except Exception as e:
            log(f"  禁忌质检失败: {e}")
            errors.append(f"taboo: {e}")
            taboo = {"risk_level": "unknown", "flags": [], "_error": str(e)}

    # 组装结果
    # profile_trace 非空时至少 needs_review
    _trace = creation.get("profile_trace", {}) if creation else {}
    _trace_clean = (
        not _trace.get("invalid_ids")
        and not _trace.get("taboo_ids")
        and not _trace.get("empty_reference")
    )
    if creation and fidelity:
        if (
            verified_recovery_rate is not None
            and verified_recovery_rate >= FIDELITY_THRESHOLD
            and fidelity_structure_valid
            and not alignment_failed
            and (taboo or {}).get("risk_level") == "low"
            and _trace_clean
        ):
            final_status = "pass"
        else:
            final_status = "needs_review"
    elif creation:
        final_status = "needs_review"
    else:
        final_status = "error"

    timings["total_ms"] = round((time.time() - t_start) * 1000)

    _telemetry.log({
        "event": "localize",
        "market": market_code,
        "final_status": final_status,
        "fidelity_retries": fidelity_retries,
        "errors": errors,
        "timings": timings,
    })

    return {
        "market": profile["market"],
        "profile_version": profile["version"],
        "source_text": source_text,
        "elements": elements if creation else None,
        "copy": creation.get("copy", "") if creation else "",
        "copy_zh": creation.get("copy_zh", "") if creation else "",
        "adaptation_note": creation.get("adaptation_note", "") if creation else "",
        "used_entries": creation.get("used_entries", []) if creation else [],
        "profile_trace": creation.get("profile_trace", {}) if creation else {},
        "fidelity": fidelity,
        "taboo": taboo,
        "final_status": final_status,
        "errors": errors if errors else None,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LocalPipe 创意本地化管线")
    parser.add_argument("--gen-hashes", action="store_true", help="生成/更新画像文件 SHA256 基线")
    parser.add_argument("--market", default="th", help="目标市场代码（默认 th）")
    parser.add_argument("--source", type=str, help="源文案文本")
    args = parser.parse_args()

    if args.gen_hashes:
        gen_profile_hashes()
        sys.exit(0)

    args.market = validate_market_code(args.market)
    demo_creative = args.source if args.source else (
        "这个夏天，别让手机先中暑！CoolClip散热背夹，3秒降温15度，"
        "开黑五连坐照样稳如老狗。学生党福音，一杯奶茶钱，游戏体验直接起飞。"
    )
    brand = load_brand_context()
    output = localize(demo_creative, args.market, brand=brand)
    print("\n" + "=" * 50)
    print(json.dumps(output, ensure_ascii=False, indent=2))

    out_path = os.path.join(BASE_DIR, "examples", f"{args.market}_demo.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n样例已保存: {out_path}")
