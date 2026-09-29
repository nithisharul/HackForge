"""Recommended actions: tells the on-call person WHAT TO DO, in plain language.

Rule-based on purpose: every recommendation is predictable, reviewable and
easy to edit, which matters more at 3am than cleverness. Recommendations come
from four kinds of evidence, most urgent first:

  1. Severity / lifecycle  - CRITICAL needs an owner now; RESOLVED needs a wrap-up.
  2. Log content           - the error templates behind the spike (timeouts,
                             database refused, out of memory, disk full, ...).
  3. Anomaly shape         - traffic dropped / surged, brand-new messages,
                             error rate rising with no known pattern.
  4. Incident history      - escalating, or still unresolved after N minutes.

To add a playbook entry, append to PLAYBOOK below: keywords to look for in the
suspicious log templates, and the steps an operator should take.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Play:
    id: str
    keywords: tuple[str, ...]
    title: str
    steps: tuple[str, ...]


# ---- log-content playbook (matched against the suspicious templates) -------
PLAYBOOK: list[Play] = [
    Play("dependency_timeout", ("timeout", "timed out", "deadline exceeded"),
         "A service we depend on is responding too slowly{target}",
         ("Check the status / latency dashboard of{target_short} the slow service.",
          "Ask: was it deployed or changed recently? If yes, contact its owner or roll it back.",
          "If it is an external provider, check their status page.",
          "Short-term relief: enable a fallback or show users a 'try again later' message.")),
    Play("database_down", ("connection refused", "db-primary", "database", "could not connect"),
         "The database is unreachable",
         ("Check the database host is up and accepting connections (health check / console).",
          "Check the connection pool isn't exhausted and credentials haven't expired.",
          "If the primary is down, fail over to the replica following the DB runbook.",
          "Tell the team: writes may be failing until this is fixed.")),
    Play("out_of_memory", ("outofmemory", "out of memory", "heap space", "oom"),
         "A process is running out of memory",
         ("Restart the affected worker(s) to restore service quickly.",
          "Look at the memory graph: a steady climb since a deploy suggests a memory leak.",
          "If it came with a recent release, roll back; otherwise add memory / instances.")),
    Play("disk_full", ("disk quota", "no space left", "disk full", "quota exceeded"),
         "A disk is full",
         ("Free space now: delete or archive old logs and temp files on the affected volume.",
          "Check log rotation is working (a runaway log file is the usual cause).",
          "Expand the volume if it is simply too small.")),
    Play("auth_failures", ("authentication failed", "invalid token", "unauthorized", "forbidden", "401", "403"),
         "Many logins / API calls are being rejected",
         ("Check whether signing keys or secrets were rotated recently (tokens may all be invalid).",
          "Check the auth service is healthy.",
          "If failures come from a few IP addresses, it may be an attack: rate-limit or block them.")),
    Play("circuit_breaker", ("circuit breaker",),
         "A circuit breaker has opened to protect a failing dependency",
         ("Find which dependency the breaker protects (it is named in the log line) and check its health.",
          "The breaker closes automatically once the dependency recovers; don't force it closed.")),
    Play("bad_requests", ("failed to parse", "unexpected token", "bad request", "malformed", "invalid json"),
         "Clients are sending requests the server can't understand",
         ("Check whether a client app or API version was released recently (version mismatch).",
          "Look at the sample lines to identify which client / endpoint is affected.")),
]

_TARGET_RE = re.compile(r"(?:calling|to|from)\s+([A-Za-z][\w.-]*(?:-service|-gateway|-api|gateway|service))", re.I)

LIFECYCLE_CRITICAL = {
    "id": "take_ownership",
    "title": "Take ownership now",
    "steps": [
        "Acknowledge this alert so others know someone is on it.",
        "Post in the team incident channel: what is broken and that you are investigating.",
        "If you are not sure what to do within 10 minutes, call the senior on-call engineer.",
    ],
}


def _rec(rid: str, title: str, steps, reason: str, kind: str) -> dict:
    return {"id": rid, "title": title, "steps": list(steps), "reason": reason, "kind": kind}


def recommend(*, severity: str, status: str, action: str, error_rate: float,
              baseline_mean: float | None, top_features: list[dict], root_cause: list[dict],
              consecutive: int, incident_alerts: int = 1, minutes_open: float = 0.0,
              max_items: int = 4) -> list[dict]:
    """Build an ordered list of recommended actions for one alert."""
    recs: list[dict] = []

    # 1) lifecycle -------------------------------------------------------------
    if status == "RESOLVED":
        return [_rec("wrap_up", "Wrap up the incident", [
            "Watch the dashboard for a few more minutes to confirm it stays normal.",
            "Write 2-3 lines in the incident channel: what happened, what fixed it, how long it lasted.",
            "Answer 'Did your actions fix it?' in the Incident response panel so the system learns the fix.",
            "If a temporary fix was applied (restart, rollback), create a ticket for the permanent fix.",
        ], "The anomaly has cleared.", "lifecycle")]

    if severity == "CRITICAL":
        recs.append(_rec(LIFECYCLE_CRITICAL["id"], LIFECYCLE_CRITICAL["title"],
                         LIFECYCLE_CRITICAL["steps"],
                         "Severity is CRITICAL: users are very likely affected right now.", "lifecycle"))

    # 2) log content -------------------------------------------------------------
    templates = [r for r in root_cause if r.get("error_count") or r.get("is_new")]
    matched_any = False
    content: list[tuple[int, dict]] = []
    for play in PLAYBOOK:
        hits = [r for r in templates if any(k in r["template"].lower() for k in play.keywords)]
        if not hits:
            continue
        matched_any = True
        m = next((_TARGET_RE.search(h["template"]) for h in hits if _TARGET_RE.search(h["template"])), None)
        target = f" ({m.group(1)})" if m else ""
        title = play.title.format(target=target)
        steps = [s.format(target_short=f" {m.group(1)}," if m else "") for s in play.steps]
        top = hits[0]
        reason = (f"'{top['template']}' appeared {top['count']} times in the last minute"
                  + (" and has never been seen before." if top.get("is_new")
                     else f" ({top['lift']}x more than usual)." if top.get("lift") else "."))
        content.append((top["count"], _rec(play.id, title, steps, reason, "log_content")))
    # keep the 2 loudest causes so the list stays short enough to act on
    content.sort(key=lambda x: x[0], reverse=True)
    recs += [r for _, r in content[:2]]

    # 3) anomaly shape --------------------------------------------------------------
    feats = {f["feature"]: f for f in top_features}
    vol = feats.get("volume")
    if vol and vol.get("direction") == "below":
        recs.append(_rec("traffic_drop", "Log traffic has dropped sharply", [
            "Check the service is still running (health endpoint, process list, container status).",
            "Check the load balancer / upstream: is traffic still reaching the service?",
            "Check the log shipping agent: the service may be fine but its logs aren't arriving.",
        ], f"Only {vol['value']:.0f} lines in the last minute vs about {vol['usual']:.0f} normally. "
           "A service that goes quiet is often down or stuck.", "shape"))
    elif vol and vol.get("direction") == "above":
        recs.append(_rec("traffic_surge", "Traffic is far above normal", [
            "Check whether it's real users (a campaign/launch) or bots / a retry storm (same IPs, same endpoint).",
            "Make sure autoscaling is keeping up; add capacity if CPU or latency is climbing.",
            "If it's abusive traffic, apply rate limiting.",
        ], f"{vol['value']:.0f} lines in the last minute vs about {vol['usual']:.0f} normally.", "shape"))

    if "new_templates" in feats and not matched_any:
        recs.append(_rec("new_messages", "New kinds of log messages appeared", [
            "New messages usually follow a deploy or config change - check what changed in the last hour.",
            "Read the new messages in the 'Why?' section; if they are errors, consider rolling back.",
        ], "The system is logging messages it has never produced before.", "shape"))

    if not matched_any and error_rate > (baseline_mean or 0) * 2 and error_rate >= 0.05:
        recs.append(_rec("generic_errors", "Error rate is up - find what changed", [
            "Check recent deployments and config changes; if the spike started right after one, roll it back.",
            "Open 'Why?' and read the sample error lines to see which service/endpoint is failing.",
            "Check the health of the services named in those lines.",
        ], f"Error rate is {error_rate:.0%} vs a normal {baseline_mean or 0:.1%}, "
           "and it doesn't match a known pattern.", "shape"))

    # 4) incident history -------------------------------------------------------------
    if action == "ESCALATED":
        recs.append(_rec("escalate", "It's getting worse - escalate", [
            "Call the senior on-call engineer / team lead now.",
            "If a change was made in the last hour, roll it back rather than debugging further.",
        ], f"Severity rose to {severity} during this incident.", "history"))
    elif action == "REMINDER" and minutes_open >= 5:
        recs.append(_rec("still_open", f"Still unresolved after {minutes_open:.0f} minutes", [
            "If the current fix isn't working, escalate to the senior on-call engineer.",
            "Post a status update in the incident channel.",
        ], f"This incident has produced {incident_alerts} alerts so far.", "history"))
    elif consecutive >= 6 and severity in ("LOW", "MEDIUM"):
        recs.append(_rec("slow_burn", "A slow-burning problem", [
            "It isn't an outage yet, but it has lasted several minutes - investigate before it grows.",
            "Look for a gradual cause: a leak, a filling disk, a slowly degrading dependency.",
        ], f"Anomalous for {consecutive} windows in a row.", "history"))

    if not recs:
        recs.append(_rec("investigate", "Investigate the unusual pattern", [
            "Open 'Why?' to see which measurements are off and the sample log lines.",
            "Check recent deployments and config changes.",
            "If it clears by itself in a few minutes, mark it as a false alarm or a real issue.",
        ], "The detectors flagged unusual behaviour without a clear known cause.", "shape"))

    # Keep the list short. Escalation / still-open advice is never dropped:
    # it is the most important thing an inexperienced operator can be told.
    if len(recs) > max_items:
        history = [r for r in recs if r["kind"] == "history"]
        others = [r for r in recs if r["kind"] != "history"]
        recs = others[:max_items - len(history)] + history
    return recs
