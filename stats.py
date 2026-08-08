"""遥测可视化 — 读 telemetry.jsonl，输出管线健康摘要"""
import json
import os
import sys
from collections import defaultdict

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_FILE = os.path.join(BASE_DIR, ".cache", "telemetry.jsonl")


def load_entries(n=20):
    if not os.path.isfile(TELEMETRY_FILE):
        return []
    entries = []
    with open(TELEMETRY_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries[-n:]


def main():
    entries = load_entries()
    if not entries:
        print("暂无遥测数据。运行 pipeline.py 后自动生成。")
        return

    # 按事件类型分组
    by_event = defaultdict(list)
    for e in entries:
        by_event[e.get("event", "unknown")].append(e)

    print(f"遥测范围: 最近 {len(entries)} 条记录\n")

    # 管线运行统计
    pipeline_runs = by_event.get("localize", [])
    if pipeline_runs:
        passed = sum(1 for r in pipeline_runs if r.get("final_status") == "pass")
        errored = sum(1 for r in pipeline_runs if r.get("final_status") == "error")

        # 区分缓存命中(总耗时 <100ms)与真实 LLM 调用，避免缓存把耗时均值拉成假数字
        def _total(r):
            return r.get("timings", {}).get("total_ms", 0) or 0

        real_runs = [r for r in pipeline_runs if _total(r) >= 100]
        cached_runs = [r for r in pipeline_runs if _total(r) < 100]

        print(f"管线运行: {len(pipeline_runs)} 次（真实 LLM 调用 {len(real_runs)}，缓存命中 {len(cached_runs)}）")
        print(f"  通过: {passed}  需审核: {len(pipeline_runs) - passed - errored}  错误: {errored}")
        print(f"  成功率: {passed / len(pipeline_runs):.0%}")

        if real_runs:
            ts = sorted(_total(r) for r in real_runs)
            avg = sum(ts) / len(ts)
            med = ts[len(ts) // 2]
            print(f"  真实调用平均耗时: {avg:.0f}ms  中位: {med:.0f}ms  最快: {min(ts):.0f}ms  最慢: {max(ts):.0f}ms")
            real_passed = sum(1 for r in real_runs if r.get("final_status") == "pass")
            print(f"  真实调用通过率: {real_passed / len(real_runs):.0%} ({real_passed}/{len(real_runs)})")

        # 各层平均耗时（仅真实调用）
        layers = ["deconstruct_ms", "recreate_ms", "fidelity_ms", "taboo_ms"]
        labels = ["解构", "重创作", "回检", "禁忌"]
        print(f"\n各层平均耗时（仅真实调用）:")
        for layer, label in zip(layers, labels):
            vals = [r.get("timings", {}).get(layer, 0) for r in real_runs if r.get("timings", {}).get(layer)]
            if vals:
                print(f"  {label}: {sum(vals)/len(vals):.0f}ms")

        # 保真重试统计
        retries = [r.get("fidelity_retries", 0) for r in pipeline_runs]
        if sum(retries) > 0:
            print(f"\n保真重试: 共 {sum(retries)} 次，人均 {sum(retries)/len(retries):.1f}")

        # 错误详情
        errors = [r for r in pipeline_runs if r.get("errors")]
        if errors:
            print(f"\n错误记录 ({len(errors)} 条):")
            for r in errors[-3:]:
                print(f"  [{r.get('market', '?')}] {r.get('errors')}")

    # 缓存统计（从遥测中无法获取缓存命中，提示用 llm_cache.json）
    cache_file = os.path.join(BASE_DIR, ".cache", "llm_cache.json")
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache_data = json.load(f)
            print(f"\n缓存条目: {len(cache_data)}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
