"""A组对照：裸Prompt本地化（无解构/无画像/无回检）
产出与管线同格式，供盲测混排。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import _llm_json, load_profile, load_dotenv

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def localize_baseline(source_text, market_code):
    """裸Prompt直出：只告诉模型目标市场，不给画像、不解构、不回检"""
    profile = load_profile(market_code)
    market = profile["market"]
    language = profile["language"]

    prompt = f"""你是{market}人。把下面的中国营销文案改成{market}本地化的版本，用{language}写，要求一定要符合本地人的表达习惯，提高顾客购买率。

【文案】
{source_text}

输出 JSON：
{{
  "copy": "本地化文案（{language}）",
  "copy_zh": "中文回译"
}}"""
    result = _llm_json(prompt, max_tokens=600)
    return {
        "group": "A_baseline",
        "market": market,
        "source_text": source_text,
        "copy": result.get("copy", ""),
        "copy_zh": result.get("copy_zh", ""),
    }


if __name__ == "__main__":
    demo = (
        "这个夏天，别让手机先中暑！CoolClip散热背夹，3秒降温15度，"
        "开黑五连坐照样稳如老狗。学生党福音，一杯奶茶钱，游戏体验直接起飞。"
    )
    out = localize_baseline(demo, "th")
    print(json.dumps(out, ensure_ascii=False, indent=2))
