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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import localize, load_brand_context, load_dotenv
from baseline import localize_baseline

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _process_one(creative, market_code, brand, with_baseline):
    """处理单个 (创意, 市场) 对，返回 (results, blind_items)"""
    cid = creative.get("id", "")
    text = creative["text"]
    results = []
    blind_items = []
    print(f"  开始: 创意 {cid} → {market_code}")

    # B组：管线
    try:
        b_result = localize(text, market_code, brand=brand)
        b_result["group"] = "B_pipeline"
        b_result["creative_id"] = cid
        results.append(b_result)
        blind_items.append({
            "sample_id": None,
            "market": market_code,
            "copy": b_result["copy"],
            "_group": "B",
            "_creative_id": cid,
        })
    except Exception as e:
        print(f"  B组失败 [{cid}→{market_code}]: {e}")

    # A组：裸Prompt
    if with_baseline:
        try:
            a_result = localize_baseline(text, market_code)
            a_result["creative_id"] = cid
            results.append(a_result)
            blind_items.append({
                "sample_id": None,
                "market": market_code,
                "copy": a_result["copy"],
                "_group": "A",
                "_creative_id": cid,
            })
        except Exception as e:
            print(f"  A组失败 [{cid}→{market_code}]: {e}")

    return results, blind_items


def run_batch(creatives_path, market_codes, with_baseline=True, workers=3):
    with open(creatives_path, encoding="utf-8") as f:
        creatives = json.load(f)

    brand = load_brand_context()
    ts = datetime.now().strftime("%m%d_%H%M")
    os.makedirs(os.path.join(BASE_DIR, "outputs"), exist_ok=True)

    full_results = []
    blind_items = []
    total = len(creatives) * len(market_codes)

    tasks = [(c, mc) for c in creatives for mc in market_codes]
    actual_workers = min(workers, total) if total > 0 else 1

    print(f"批量: {len(creatives)} 创意 × {len(market_codes)} 市场 = {total} 任务，并发数 {actual_workers}")

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {
            executor.submit(_process_one, c, mc, brand, with_baseline): (c.get("id", ""), mc)
            for c, mc in tasks
        }

        done = 0
        for future in as_completed(futures):
            cid, mc = futures[future]
            done += 1
            try:
                r, b = future.result()
                full_results.extend(r)
                blind_items.extend(b)
                print(f"  [{done}/{total}] 完成: {cid} → {mc}")
            except Exception as e:
                print(f"  [{done}/{total}] 失败: {cid} → {mc}: {e}")

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
    import argparse
    parser = argparse.ArgumentParser(description="LocalPipe 批量本地化 + 盲测集生成")
    parser.add_argument("creatives", nargs="?", default=os.path.join(BASE_DIR, "examples", "creatives.json"),
                        help="创意 JSON 文件路径")
    parser.add_argument("markets", nargs="?", default="th",
                        help="目标市场代码，逗号分隔（默认 th）")
    parser.add_argument("--workers", "-w", type=int, default=3,
                        help="并发数（默认 3）")
    parser.add_argument("--no-baseline", action="store_true",
                        help="跳过 A 组裸 Prompt")
    args = parser.parse_args()
    run_batch(args.creatives, args.markets.split(","),
              with_baseline=not args.no_baseline, workers=args.workers)
