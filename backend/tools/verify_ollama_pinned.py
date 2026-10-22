"""Ops diagnostic: confirm the pinned model stays resident and stays fast.

Runs N sequential generations and reports each one's load time. With
``keep_alive=-1`` every call after the first should report ~0s load; without it
the model is evicted and a later call silently pays a full reload.

Usage:  python tools/verify_ollama_pinned.py [calls]
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "aya:8b")
ARABIC = "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643\u0645 \u0641\u064a \u0645\u0633\u062c\u062f\u0646\u0627"


def generate(keep_alive: int | str = -1, num_ctx: int = 2048) -> dict:
    payload = {
        "model": MODEL,
        "stream": False,
        "keep_alive": keep_alive,
        "prompt": ARABIC,
        "options": {"num_ctx": num_ctx, "num_batch": 512},
    }
    req = urllib.request.Request(
        f"{ENDPOINT}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    calls = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(f"{MODEL} @ {ENDPOINT} - keep_alive=-1, num_ctx=2048\n")
    print(f"{'call':<6}{'load_s':>9}{'total_s':>10}{'gen_tok/s':>11}")
    print("-" * 36)
    for i in range(1, calls + 1):
        started = time.perf_counter()
        res = generate()
        wall = time.perf_counter() - started
        load = res.get("load_duration", 0) / 1e9
        dur = res.get("eval_duration", 0) / 1e9
        tps = (res.get("eval_count", 0) / dur) if dur else 0.0
        print(f"{i:<6}{load:>9.1f}{wall:>10.1f}{tps:>11.2f}")
    print("\nExpected with pinning: load_s ~= 0.0 on every call after the first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
