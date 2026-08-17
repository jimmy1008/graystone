# -*- coding: utf-8 -*-
"""灰石村模擬器控制台。

用法：
  python run.py --days 90                 # 純規則模式跑 90 天
  python run.py --days 30 --llm on        # 全員 LLM 模式（需 Ollama + qwen3:8b）
  python run.py --days 365 --seed 7 --quiet
"""
import argparse
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sim.engine import World
from sim.llm import LLMClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--llm", choices=["on", "off"], default="off")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    llm = None
    if args.llm == "on":
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(open(Path(__file__).parent / "data" / "params.yaml", encoding="utf-8"))["llm"]
        llm = LLMClient(cfg)
        if not llm.available():
            print("⚠️  Ollama 不可用，退化為純規則模式")
            llm = None
        else:
            print(f"🧠 LLM 模式：{cfg['model']}（全員接 LLM，抉擇點預算 {cfg['max_decisions_per_day']}/日）")

    w = World(seed=args.seed, llm=llm)
    t0 = time.time()
    for _ in range(args.days):
        notable = w.tick()
        if not args.quiet:
            for ev in notable:
                print(f"  Y{w.year} 第{w.doy:>3}天[{w.season}|{w.weather}] {ev['text']}")
        if w.day % 30 == 0:
            s = w.snapshot()
            print(f"── 第 {w.day} 天 ── 人口 {s['population']} | 村庫 {s['treasury']}G | "
                  f"貨幣總量 {s['money_total']}G (外部淨流入 {s['external_net']}) | "
                  f"糧倉穀物 {s['granary'].get('穀物', 0)}")
    w.close()
    s = w.snapshot()
    print(f"\n=== 模擬結束：{args.days} 天，耗時 {time.time()-t0:.1f}s ===")
    print(f"人口 {s['population']}/30 | 村庫 {s['treasury']}G | 貨幣 {s['money_total']}G | LLM 呼叫 {s['llm_calls']}（失敗 {s['llm_failures']}）")
    print(f"事件史 → out/events.jsonl；末態 → out/state.json")


if __name__ == "__main__":
    main()
