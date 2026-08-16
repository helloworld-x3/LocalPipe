"""批量模式：多条创意 × 多市场，A/B两组产出，供对照实验
用法:
  python batch.py creatives.json th,jp,us
产出:
  outputs/batch_<时间戳>.json   全量结果（含追溯质检数据，B组）
  outputs/blind_<时间戳>.json   盲测集（A/B混排去标识，评审用）
  outputs/key_<时间戳>.json     揭盲对照表（评审结束前不发）
  outputs/skipped_<时间戳>.json 未进入盲测的 A/B 配对及原因
"""
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import localize, load_dotenv
from baseline import localize_baseline

load_dotenv()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _process_one(creative, market_code, brand, with_baseline):
    """处理单个 (创意, 市场) 对，A/B 严格配对进盲测"""
    cid = creative.get("id", "")
    text = creative["text"]
    results = []
    blind_items = []
    skipped_pairs = []
    print(f"  开始: 创意 {cid} → {market_code}")

    # 先分别计算 A、B
    b_result = None
    a_result = None

    try:
        b_result = localize(text, market_code, brand=brand)
        b_result["group"] = "B_pipeline"
        b_result["creative_id"] = cid
        results.append(b_result)
    except Exception as e:
        print(f"  B组异常 [{cid}→{market_code}]: {e}")
        b_result = {"copy": "", "final_status": "error", "_error": str(e)}

    if with_baseline:
        try:
            a_result = localize_baseline(text, market_code)
            a_result["creative_id"] = cid
            results.append(a_result)
        except Exception as e:
            print(f"  A组异常 [{cid}→{market_code}]: {e}")
            a_result = {"copy": "", "_error": str(e)}

    # 严格配对：A和B都非空且B非error才一起进盲测
    b_ok = bool(b_result) and bool(b_result.get("copy")) and b_result.get("final_status") != "error"
    if with_baseline:
        a_ok = bool(a_result) and bool(a_result.get("copy"))
    else:
        a_ok = True

    if with_baseline and a_ok and b_ok:
        blind_items.append({
            "sample_id": None,
            "market": market_code,
            "copy": b_result["copy"],
            "_group": "B",
            "_creative_id": cid,
        })
        blind_items.append({
            "sample_id": None,
            "market": market_code,
            "copy": a_result["copy"],
            "_group": "A",
            "_creative_id": cid,
        })
    elif with_baseline:
        reason = []
        if not b_ok:
            reason.append(f"B:final_status={b_result.get('final_status') if b_result else 'N/A'}")
        if not a_ok:
            reason.append("A:copy空")
        pair_key = f"{cid}→{market_code}"
        skipped_pairs.append({"pair": pair_key, "reason": "; ".join(reason)})
        print(f"  整对跳过盲测 [{pair_key}]: {skipped_pairs[-1]['reason']}")
    elif not with_baseline and b_ok:
        blind_items.append({
            "sample_id": None,
            "market": market_code,
            "copy": b_result["copy"],
            "_group": "B",
            "_creative_id": cid,
        })

    return results, blind_items, skipped_pairs


def run_batch(creatives_path, market_codes, with_baseline=True, workers=3):
    with open(creatives_path, encoding="utf-8") as f:
        creatives = json.load(f)

    # 实验批次不注入全局品牌上下文：brand_context.json 是 CoolClip 专属，
    # 套到多产品批次会污染每条创意（口红/坚果被强制改成 CoolClip）
    brand = None
    ts = datetime.now().strftime("%m%d_%H%M%S")
    os.makedirs(os.path.join(BASE_DIR, "outputs"), exist_ok=True)

    full_results = []
    blind_items = []
    all_skipped = []
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
                r, b, skipped = future.result()
                full_results.extend(r)
                blind_items.extend(b)
                all_skipped.extend(skipped)
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
    for name, data in [("batch", full_results), ("blind", blind), ("key", key), ("skipped", all_skipped)]:
        p = os.path.join(BASE_DIR, "outputs", f"{name}_{ts}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        paths[name] = p

    if all_skipped:
        print(f"\n跳过配对: {len(all_skipped)} 对")
        for s in all_skipped:
            print(f"  {s['pair']}: {s['reason']}")

    print(f"\n完成: {len(full_results)} 条产出, {len(blind_items)} 条入盲测")
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
