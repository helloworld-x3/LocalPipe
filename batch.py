"""批量模式：多条创意 × 多市场，A/B两组产出，供对照实验
用法:
  python batch.py creatives.json th,jp,us
产出:
  outputs/batch_<时间戳>.json   全量结果（含追溯质检数据，B组）
  outputs/blind_<时间戳>.json   盲测集（A/B混排去标识，评审用）
  outputs/key_<时间戳>.json     揭盲对照表（评审结束前不发）
"""
import json
import os
import random
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import localize, load_brand_context, load_dotenv
from baseline import localize_baseline

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_batch(creatives_path, market_codes, with_baseline=True):
    with open(creatives_path, encoding="utf-8") as f:
        creatives = json.load(f)

    brand = load_brand_context()
    ts = datetime.now().strftime("%m%d_%H%M")
    os.makedirs(os.path.join(BASE_DIR, "outputs"), exist_ok=True)

    full_results = []
    blind_items = []

    total = len(creatives) * len(market_codes)
    done = 0
    for c in creatives:
        cid = c.get("id", "")
        text = c["text"]
        for mc in market_codes:
            done += 1
            print(f"\n===== [{done}/{total}] 创意 {cid} → {mc} =====")

            # B组：管线
            try:
                b_result = localize(text, mc, brand=brand)
                b_result["group"] = "B_pipeline"
                b_result["creative_id"] = cid
                full_results.append(b_result)
                blind_items.append({
                    "sample_id": None,  # 混排后统一编号
                    "market": mc,
                    "copy": b_result["copy"],
                    "_group": "B",
                    "_creative_id": cid,
                })
            except Exception as e:
                print(f"  B组失败: {e}")

            # A组：裸Prompt
            if with_baseline:
                try:
                    a_result = localize_baseline(text, mc)
                    a_result["creative_id"] = cid
                    full_results.append(a_result)
                    blind_items.append({
                        "sample_id": None,
                        "market": mc,
                        "copy": a_result["copy"],
                        "_group": "A",
                        "_creative_id": cid,
                    })
                except Exception as e:
                    print(f"  A组失败: {e}")

            time.sleep(1)

    # 盲测集：打乱 + 去标识
    random.shuffle(blind_items)
    key = []
    blind = []
    for i, item in enumerate(blind_items, 1):
        sid = f"S{i:03d}"
        key.append({
            "sample_id": sid,
            "group": item["_group"],
            "creative_id": item["_creative_id"],
            "market": item["market"],
        })
        blind.append({
            "sample_id": sid,
            "market": item["market"],
            "copy": item["copy"],
        })

    paths = {}
    for name, data in [("batch", full_results), ("blind", blind), ("key", key)]:
        p = os.path.join(BASE_DIR, "outputs", f"{name}_{ts}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        paths[name] = p

    print(f"\n完成: {len(full_results)} 条产出")
    for name, p in paths.items():
        print(f"  {name}: {p}")
    return paths


if __name__ == "__main__":
    creatives_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "examples", "creatives.json")
    markets = (sys.argv[2] if len(sys.argv) > 2 else "th").split(",")
    run_batch(creatives_file, markets)
