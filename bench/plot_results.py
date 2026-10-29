#!/usr/bin/env python
"""Render the PLAN2 results into figures: a recall bar per arm and a recall-vs-tokens scatter.

Reads whatever `bench/research/<benchmark>/<arm>.json` files exist (produced by research_eval.py) and
writes PNGs under `bench/research/`. Robust to missing arms — it plots only what has been run, so it is
useful mid-experiment. Token-free; matplotlib only.

    python bench/plot_results.py [--dir bench/research]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path



def _plt():
    """matplotlib, imported only when a figure is drawn -- the arm/label helpers (and bench/prove.py)
    stay importable without it."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt

_ARM_LABEL = {"A": "Local+Orion", "B": "Local, no Orion", "C": "Opus, no Orion", "D": "Semgrep"}
_ARM_ORDER = ["A", "B", "C", "D"]


def _is_arm(stem: str) -> bool:
    """A-D, plus any Orion+Claude result: arm O saved as O.json or O-<model>.json."""
    return stem in _ARM_LABEL or stem == "O" or stem.startswith("O-")


def _label(stem: str, result: dict | None = None) -> str:
    if stem in _ARM_LABEL:
        return _ARM_LABEL[stem]
    model = (result or {}).get("model") or stem.partition("-")[2] or "Claude"
    return f"Orion + {model}"


def _arms(data: dict) -> list[str]:
    """Every arm present in any benchmark: A-D in their fixed order, then the O* runs sorted."""
    present = {a for b in data.values() for a in b}
    return [a for a in _ARM_ORDER if a in present] + sorted(a for a in present if a not in _ARM_LABEL)


def _load(root: Path) -> dict[str, dict[str, dict]]:
    """benchmark -> arm -> result dict, for every result JSON present."""
    out: dict[str, dict[str, dict]] = {}
    for bench_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(bench_dir.glob("*.json")):
            arm = f.stem
            if not _is_arm(arm):
                continue
            try:
                out.setdefault(bench_dir.name, {})[arm] = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return out


def _recall_bars(data: dict, out_path: Path) -> None:
    benches = sorted(data)
    arms = _arms(data)
    if not benches or not arms:
        return
    plt = _plt()
    fig, ax = plt.subplots(figsize=(1.6 * len(arms) + 2, 4.2))
    width = 0.8 / max(1, len(benches))
    for bi, b in enumerate(benches):
        xs = [i + bi * width for i in range(len(arms))]
        ys = [data[b].get(a, {}).get("recall", 0) for a in arms]
        bars = ax.bar(xs, ys, width=width, label=b)
        for x, a in zip(xs, arms):
            r = data[b].get(a)
            if r:
                ax.text(x, r.get("recall", 0) + 0.1, f"{r.get('recall',0)}/{r.get('total','?')}",
                        ha="center", va="bottom", fontsize=8)
    ax.set_xticks([i + width * (len(benches) - 1) / 2 for i in range(len(arms))])
    ax.set_xticklabels([_label(a, next((data[b][a] for b in benches if a in data[b]), None))
                        for a in arms], rotation=15, ha="right")
    ax.set_ylabel("vulns found (recall)")
    ax.set_title("Recall by arm")
    ax.legend(title="benchmark")
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def _recall_vs_tokens(data: dict, out_path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    plotted = False
    for b in sorted(data):
        for a in _arms(data):
            r = data[b].get(a)
            if not r:
                continue
            toks = r.get("tokens", {}).get("total", {}).get("total_tokens", 0)
            if toks <= 0:
                continue                       # semgrep etc. — no tokens, skip the token axis
            ax.scatter(toks, r.get("recall", 0), s=70)
            ax.annotate(f"{_label(a, r)}·{b}", (toks, r.get("recall", 0)),
                        textcoords="offset points", xytext=(6, 4), fontsize=8)
            plotted = True
    if not plotted:
        plt.close(fig)
        return
    ax.set_xscale("log")
    ax.set_xlabel("total tokens (log)")
    ax.set_ylabel("vulns found (recall)")
    ax.set_title("Recall vs token spend — up-and-left is better")
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="bench/research")
    args = ap.parse_args(argv)
    root = Path(args.dir)
    if not root.exists():
        print(f"no results dir at {root}")
        return 1
    data = _load(root)
    if not data:
        print(f"no result JSONs under {root}")
        return 1
    _recall_bars(data, root / "recall_by_arm.png")
    _recall_vs_tokens(data, root / "recall_vs_tokens.png")
    print(f"wrote {root/'recall_by_arm.png'} and {root/'recall_vs_tokens.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
