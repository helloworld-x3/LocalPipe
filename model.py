"""模型抽象层 — 多厂家切换 + 流式输出"""

from openai import OpenAI
import os
import re
import sys
import json
import time
import hashlib
import threading

MAX_RETRIES = 2

# ========== 速率限制（令牌桶） ==========

class RateLimiter:
    """令牌桶：限制 API 调用频率，防费用刷空"""

    def __init__(self, rate=3.0, burst=5):
        self.rate = rate          # 令牌填充速率（个/秒）
        self.burst = burst        # 桶容量（允许瞬时突发）
        self.tokens = float(burst)
        self.last_fill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_fill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_fill = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            wait = (1.0 - self.tokens) / self.rate
            time.sleep(wait)
            self.tokens = 0.0
            self.last_fill = time.monotonic()

# 全局速率限制器实例
_rate_limiter = RateLimiter(rate=3.0, burst=5)

# Prompt 注入防御：
# 主要防线：XML 分隔符 + HTML 实体转义，将用户输入与指令在结构上隔离
# 辅助防线：正则模式匹配——不可靠（LLM 自然语言理解，任何规则都可绕过），仅作预警层
_INJECTION_PATTERNS = [
    r"(?:忽略|无视|忘记|不要).{0,10}(?:以上|之前|前面|所有|系统).{0,10}(?:指令|提示|规则|要求)",
    r"(?:输出|显示|打印|泄露|告诉我).{0,10}(?:系统.{0,5})?(?:prompt|提示词|指令|规则|设定)",
    r"(?:你是|你现在是|你变成|扮演).{0,15}(?:而不是|不再是)",
    r"(?:DAN|Developer Mode|jailbreak)",
]

# 需要从用户输入中彻底移除的 token（防分隔符注入）
_BLOCKED_TOKENS = [
    "<user_input>", "</user_input>",
    "<|im_start|>", "<|im_end|>",
    "<|endoftext|>",
]


def sanitize_user_input(text):
    """清洗用户输入：结构隔离（主）+ 模式预警（辅）

    核心策略：对用户输入做 HTML 实体转义（< → &lt;, > → &gt;）后包裹
    在 <user_input> 标签内，从结构上防止用户文本被 LLM 误解为指令。
    正则匹配仅作低成本预警，不应视为可靠防线。
    """
    if not text or not isinstance(text, str):
        return text

    # 辅助：高风险模式预警（可绕过，不作为唯一防线）
    for pat in _INJECTION_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            raise ValueError(f"输入包含疑似指令注入内容，已拦截")

    # 主防线：迭代清除分隔符 token（防嵌套/拼接绕过），直到稳定
    sanitized = text
    changed = True
    while changed:
        changed = False
        for token in _BLOCKED_TOKENS:
            if token in sanitized:
                sanitized = sanitized.replace(token, "")
                changed = True

    # HTML 实体转义：用户输入中的 < > 变成 &lt; &gt;，无法形成 XML 标签
    sanitized = sanitized.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return f"<user_input>\n{sanitized}\n</user_input>"


class ModelConfig:
    def __init__(self):
        self.api_key = (
            os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self.base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
        self.model = os.environ.get("LLM_MODEL", "deepseek-chat")

    @property
    def provider_name(self):
        if "deepseek" in self.base_url:
            return "DeepSeek"
        if "dashscope" in self.base_url or "aliyun" in self.base_url:
            return "DashScope(Qwen)"
        if "openai" in self.base_url:
            return "OpenAI"
        return self.base_url


class ModelClient:
    """封装 OpenAI 兼容 API：流式输出、重试、多厂家切换"""

    def __init__(self, config=None):
        self.config = config or ModelConfig()
        if not self.config.api_key:
            print("[错误] 未找到 API Key，请在 .env 中设置 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY")
            sys.exit(1)
        self.client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)

    def chat_stream(self, messages, tools=None):
        """流式调用，逐 token 输出，返回 (完整文本, tool_calls)"""
        _rate_limiter.acquire()
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                stream = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    tools=tools or [],
                    stream=True,
                    timeout=60,
                )
                return self._process_stream(stream)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    safe_print(f"  [重试 {attempt + 1}/{MAX_RETRIES}] {e}")
                    time.sleep(2)
        raise last_error

    def chat_simple(self, messages, max_tokens=200):
        """非流式调用，返回纯文本。用于内部工具（如语义搜索）"""
        _rate_limiter.acquire()
        try:
            resp = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                max_tokens=max_tokens,
                timeout=30,
            )
            return resp.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}")

    def _process_stream(self, stream):
        content_parts = []
        tool_calls_data = {}  # index -> {id, name, arguments}

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            # 文本
            if delta.content:
                content_parts.append(delta.content)
                safe_print(delta.content, end="", flush=True)

            # 工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_data[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_data[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_data[idx]["arguments"] += tc.function.arguments

        if content_parts:
            safe_print()  # 换行

        text = "".join(content_parts)
        tool_calls = None
        if tool_calls_data:
            tool_calls = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls_data.values()
            ]
        return text, tool_calls


def safe_print(*args, end="\n", flush=False, **kwargs):
    """GBK 安全打印"""
    text = " ".join(str(a) for a in args) + end
    try:
        sys.stdout.write(text.encode("gbk", errors="replace").decode("gbk"))
    except Exception:
        sys.stdout.write(text)
    if flush:
        sys.stdout.flush()


# ========== LLM 响应缓存 ==========

class Cache:
    """文件级缓存：避免重复 API 调用。线程安全。"""

    def __init__(self, cache_dir=None, max_entries=1000):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
        self.cache_dir = cache_dir
        self.cache_file = os.path.join(cache_dir, "llm_cache.json")
        self._data = None
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def _load(self):
        if self._data is not None:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.isfile(self.cache_file):
            try:
                with open(self.cache_file, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        else:
            self._data = {}

    def get(self, key):
        with self._lock:
            self._load()
            entry = self._data.get(key)
            return entry["value"] if entry else None

    def set(self, key, value):
        with self._lock:
            self._load()
            self._data[key] = {"value": value, "ts": time.time()}
            if len(self._data) > self._max_entries:
                keys = sorted(self._data, key=lambda k: self._data[k]["ts"], reverse=True)
                self._data = {k: self._data[k] for k in keys[:self._max_entries]}
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)


# ========== 遥测日志 ==========

class Telemetry:
    """结构化遥测：耗时、token、成功率。JSONL 追加写入。"""

    def __init__(self, log_dir=None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
        self.log_dir = log_dir
        self.log_file = os.path.join(log_dir, "telemetry.jsonl")
        self._lock = threading.Lock()

    def log(self, entry):
        entry["ts"] = time.time()
        os.makedirs(self.log_dir, exist_ok=True)
        with self._lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
