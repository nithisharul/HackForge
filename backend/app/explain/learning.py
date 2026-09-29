"""Learning runbook: remembers what operators did to fix an incident and
suggests it the next time a similar incident appears.

1. While an incident is open, we build up its FINGERPRINT:
       templates : the unusual error message types behind it (e.g.
                   "Connection refused to db-primary <IP>")
       kind      : error_spike | traffic_drop | traffic_surge | new_messages | pattern
2. While it's open, operators record the actions they take.
3. When it resolves, the operator confirms "did your actions fix it?".
   Yes -> the action list is saved as a successful resolution.
   No  -> saved as a failed attempt (it counts against that action list).
4. For a new incident, past resolutions with a similar fingerprint are grouped
   by action list and ranked: net successes (worked - failed), then fewest
   steps, then fastest time to resolve. The top ones become "Proven fixes".

Everything here is pure functions; storage lives in db/store.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

SIMILARITY_THRESHOLD = 0.5
TEMPLATE_WEIGHT = 0.7
KIND_WEIGHT = 0.3


def anomaly_kind(top_features: list[dict], error_driven: bool) -> str:
    if error_driven:
        return "error_spike"
    top = top_features[0] if top_features else {}
    feat, direction = top.get("feature"), top.get("direction")
    if feat == "volume":
        return "traffic_drop" if direction == "below" else "traffic_surge"
    if feat in ("new_templates", "unique_templates"):
        return "new_messages"
    if feat in ("error_rate", "error_count"):
        return "error_spike"
    return "pattern"


def fingerprint(root_cause: list[dict], top_features: list[dict], error_driven: bool,
                max_templates: int = 5) -> dict:
    """Stable description of 'what kind of incident this is'.

    Uses the UNUSUAL error message types (errors that are brand-new or far above
    their usual share), not the background errors a healthy system always has.
    Falls back to any error types, then to new message types (e.g. a traffic
    drop has no errors at all)."""
    errs = [r for r in root_cause if r.get("error_count")]
    unusual = [r for r in errs if r.get("is_new") or r.get("lift", 0) >= 3]
    chosen = unusual or errs or [r for r in root_cause if r.get("is_new")]
    chosen = sorted(chosen, key=lambda r: r.get("count", 0), reverse=True)[:max_templates]
    return {"templates": sorted(r["template"] for r in chosen),
            "kind": anomaly_kind(top_features, error_driven)}


def merge_fingerprints(old: dict | None, new: dict, cap: int = 8) -> dict:
    """An incident's fingerprint grows as it develops: the first alert may only
    see the start of the failure, later alerts see all of it."""
    if not old:
        return new
    templates = sorted(set(old.get("templates", [])) | set(new.get("templates", [])))[:cap]
    kind = old.get("kind") if old.get("kind") not in (None, "pattern") else new.get("kind")
    return {"templates": templates, "kind": kind}


def similarity(a: dict, b: dict) -> float:
    """0..1. Mostly 'do they share the same error messages', partly 'same kind'.

    Template overlap uses |A∩B| / min(|A|,|B|), so an incident caught early
    (only part of its error types visible yet) still matches a past incident
    that showed the full set."""
    ta, tb = set(a.get("templates", [])), set(b.get("templates", []))
    if ta and tb:
        tmpl = len(ta & tb) / min(len(ta), len(tb))
    elif not ta and not tb:
        tmpl = 1.0 if a.get("kind") == b.get("kind") else 0.0
    else:
        tmpl = 0.0
    kind = 1.0 if a.get("kind") == b.get("kind") else 0.0
    return TEMPLATE_WEIGHT * tmpl + KIND_WEIGHT * kind


def _norm(action: str) -> str:
    return " ".join(action.lower().split())


def rank_fixes(fp: dict, resolutions: list[dict], exclude_incident: str | None = None,
               top_k: int = 2) -> list[dict]:
    """Group similar past resolutions by their action list and rank them."""
    groups: dict[tuple, dict] = {}
    for r in resolutions:
        if r.get("incident_id") == exclude_incident or not r.get("actions"):
            continue
        sim = similarity(fp, r["fingerprint"])
        if sim < SIMILARITY_THRESHOLD:
            continue
        key = tuple(_norm(a) for a in r["actions"])
        g = groups.setdefault(key, {"actions": list(r["actions"]), "worked": 0, "failed": 0,
                                    "steps": [], "ttr": [], "last_used": "", "last_operator": None,
                                    "similarity": 0.0, "incidents": []})
        if r["success"]:
            g["worked"] += 1
            g["steps"].append(r["steps"])
            g["ttr"].append(r["ttr_seconds"])
            if r["created_at"] >= g["last_used"]:
                g["last_used"], g["last_operator"] = r["created_at"], r.get("operator")
                g["actions"] = list(r["actions"])       # newest wording
        else:
            g["failed"] += 1
        g["similarity"] = max(g["similarity"], sim)
        g["incidents"].append(r["incident_id"])

    fixes = []
    for g in groups.values():
        if g["worked"] == 0 or g["worked"] <= g["failed"]:
            continue          # never worked, or fails as often as it works
        avg_steps = sum(g["steps"]) / len(g["steps"])
        avg_ttr = sum(g["ttr"]) / len(g["ttr"])
        fixes.append({
            "actions": g["actions"],
            "times_worked": g["worked"],
            "times_failed": g["failed"],
            "avg_steps": round(avg_steps, 1),
            "avg_minutes": round(avg_ttr / 60, 1),
            "last_used": g["last_used"],
            "last_operator": g["last_operator"],
            "similarity": round(g["similarity"], 2),
            "_key": (-(g["worked"] - g["failed"]), avg_steps, avg_ttr),
        })
    fixes.sort(key=lambda f: f["_key"])
    for f in fixes:
        f.pop("_key")
    return fixes[:top_k]


def seconds_between(start_iso: str, end_iso: str | None) -> float:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso) if end_iso else datetime.now(timezone.utc)
    return max((end - start).total_seconds(), 0.0)
