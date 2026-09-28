"""Root-cause hints: which log templates are over-represented right now.

For each template in the current window we compare its share of the window with
its long-run share (from the template miner). Templates that are new, or that
appear far more often than usual, and especially error templates, float to the top.
"""
from __future__ import annotations

from collections import Counter

from app.core.template_miner import TemplateMiner


def rank_templates(window_counts: Counter[int], error_counts: Counter[int],
                   miner: TemplateMiner, k: int = 5) -> list[dict]:
    total = sum(window_counts.values())
    if not total:
        return []
    rows = []
    for tid, cnt in window_counts.items():
        share_now = cnt / total
        # long-run share excluding this window's contribution
        hist_count = max(miner.counts.get(tid, 0) - cnt, 0)
        hist_total = max(miner.total - total, 1)
        share_hist = hist_count / hist_total
        lift = share_now / max(share_hist, 1.0 / hist_total)
        errors = error_counts.get(tid, 0)
        is_new = hist_count == 0
        score = lift * (1 + 4 * errors / cnt) * (2 if is_new else 1)
        rows.append({
            "template_id": tid,
            "template": miner.names.get(tid, "?"),
            "count": cnt,
            "error_count": errors,
            "share_now": round(share_now, 4),
            "share_usual": round(share_hist, 4),
            "lift": round(min(lift, 9999.0), 1),
            "is_new": is_new,
            "_score": score,
        })
    rows = [r for r in rows if r["error_count"] > 0 or r["is_new"] or r["lift"] >= 3]
    rows.sort(key=lambda r: r["_score"], reverse=True)
    for r in rows:
        r.pop("_score")
    return rows[:k]
