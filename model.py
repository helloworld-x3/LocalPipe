"""模型抽象层 — 多厂家切换 + 流式输出"""

from openai import OpenAI
import os
import sys
import json
import time

MAX_RETRIES = 2


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
