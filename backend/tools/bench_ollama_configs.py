"""Ops benchmark: measure real Ollama throughput for candidate configs.

Ops/diagnostic tool (§195). Each config is timed against the live server so a
tuning decision is based on measurements from THIS machine rather than
assumptions. Reports prompt-eval and generation tokens/sec plus the load cost,
because on a cold model the load dominates and is what an operator actually
feels.

Usage:
    ollama serve &
    python tools/bench_ollama_configs.py [model]
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434")
ARABIC = "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643\u0645 \u0641\u064a \u0645\u0633\u062c\u062f\u0646\u0627"
SYSTEM = (
    "You are a professional Arabic-to-{language} translator for a mosque "
    "sermon (khutbah). Translate the user's Arabic text into {language}. "
    "Rules: (1) output ONLY the translation, nothing else; (2) do not add "
    "explanations, commentary, transliteration, or quotation marks; (3) never "
    "cite or invent Quran/Hadith references; (4) never add religious rulings, "
    "interpretations, or opinions; (5) preserve the meaning faithfully."
)

# Each candidate isolates ONE lever so the measured delta is attributable.
CONFIGS: list[tuple[str, dict]] = [
    ("baseline (current adapter defaults)", {}),
    ("keep_alive=-1 (stay resident)", {"keep_alive": -1}),
    ("+ num_ctx=2048", {"keep_alive": -1, "num_ctx": 2048}),
    ("+ threads=8", {"keep_alive": -1, "num_ctx": 2048, "num_thread": 8}),
    ("+ batch=512", {"keep_alive": -1, "num_ctx": 2048, "num_thread": 8, "num_batch": 512}),
]


def call(model: str, options: dict, keep_alive) -> dict | None:
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM.format(language="English")},
            {"role": "user", "content": ARABIC},
        ],
        "options": dict(options),
    }
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{ENDPOINT}/api/chat",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"    FAILED: {exc}")
        return None
    wall = time.perf_counter() - start
    data["_wall_s"] = wall
    return data


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "aya:8b"
    print(f"Benchmarking {model} against {ENDPOINT}\n")
    print(f"{'config':<40} {'load_s':>8} {'wall_s':>8} {'gen_tok/s':>10}")
    print("-" * 70)
    for label, opts in CONFIGS:
        # keep_alive lives at the payload root, not inside options.
        ka = opts.get("keep_alive")
        opts.pop("keep_alive", None)
        res = call(model, opts, keep_alive=ka)
        if res is None:
            continue
        load_s = res.get("load_duration", 0) / 1e9
        wall = res["_wall_s"]
        n = res.get("eval_count", 0)
        dur = res.get("eval_duration", 0) / 1e9
        tps = (n / dur) if dur else 0.0
        out = (res.get("message", {}) or {}).get("content", "").strip()
        print(f"{label:<40} {load_s:>8.1f} {wall:>8.1f} {tps:>10.2f}")
        if len(out) > 60:
            print(f"    -> {out[:60]}...")
        else:
            print(f"    -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
