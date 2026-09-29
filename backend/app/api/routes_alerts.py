from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_store

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
async def list_alerts(
    limit: int = Query(100, ge=1, le=1000),
    min_severity: str | None = Query(None, description="LOW | MEDIUM | HIGH | CRITICAL"),
    status: str | None = Query(None, description="OPEN | ESCALATED | RESOLVED"),
    incident_id: str | None = None,
    store=Depends(get_store),
):
    try:
        return await store.list_alerts(limit, min_severity, status, incident_id)
    except KeyError:
        raise HTTPException(400, f"invalid severity {min_severity}")


@router.get("/alerts/{alert_id}")
async def get_alert(alert_id: str, store=Depends(get_store)):
    alert = await store.get_alert(alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    return alert


class Feedback(BaseModel):
    label: Literal["TP", "FP"]


@router.post("/alerts/{alert_id}/feedback")
async def feedback(alert_id: str, body: Feedback, store=Depends(get_store)):
    """Mark an alert as a true or false positive (tracks alert fatigue)."""
    if not await store.set_feedback(alert_id, body.label):
        raise HTTPException(404, "alert not found")
    return {"ok": True, "summary": await store.feedback_summary()}


# ---- incidents & operator response --------------------------------------------
@router.get("/incidents")
async def incidents(limit: int = Query(50, ge=1, le=500), store=Depends(get_store)):
    """Incidents with owner (ack_by), confirmation state and number of logged actions."""
    return await store.list_incidents(limit)


@router.get("/incidents/{incident_id}")
async def incident_detail(incident_id: str, store=Depends(get_store)):
    """Everything the Incident response panel needs: owner, fingerprint, action log,
    the latest open alert (with recommended actions and proven fixes)."""
    inc = await store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


async def _broadcast(request: Request, incident_id: str) -> dict:
    """Push the updated incident to every open dashboard."""
    inc = await request.app.state.store.get_incident(incident_id)
    await request.app.state.ws_manager.broadcast("alerts", {"type": "incident_update", "data": inc})
    return inc


class Operator(BaseModel):
    operator: str = Field(..., min_length=1, max_length=60)


@router.post("/incidents/{incident_id}/ack")
async def acknowledge(incident_id: str, body: Operator, request: Request, store=Depends(get_store)):
    """'I'm on it': take ownership (or take over) an incident."""
    if await store.get_incident(incident_id) is None:
        raise HTTPException(404, "incident not found")
    await store.acknowledge(incident_id, body.operator.strip())
    return await _broadcast(request, incident_id)


class ActionIn(BaseModel):
    operator: str = Field(..., min_length=1, max_length=60)
    action: str = Field(..., min_length=2, max_length=300)
    source: Literal["recommended", "proven", "custom"] = "custom"
    rec_id: str | None = Field(None, max_length=80)


@router.post("/incidents/{incident_id}/actions")
async def add_action(incident_id: str, body: ActionIn, request: Request, store=Depends(get_store)):
    """Record an action taken while the incident is open."""
    inc = await store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    if inc.get("confirmed") is not None:
        raise HTTPException(409, "incident already confirmed; actions are closed")
    if not inc.get("ack_by"):          # doing something = owning it
        await store.acknowledge(incident_id, body.operator.strip())
    await store.add_action(incident_id, body.operator.strip(), body.action.strip(),
                           body.source, body.rec_id)
    return await _broadcast(request, incident_id)


class ResolutionIn(BaseModel):
    operator: str = Field(..., min_length=1, max_length=60)
    fixed: bool


@router.post("/incidents/{incident_id}/resolution")
async def confirm_resolution(incident_id: str, body: ResolutionIn, request: Request,
                             store=Depends(get_store)):
    """'Did your actions fix it?' Yes saves the actions as a proven fix for
    similar incidents; No records them as not effective."""
    try:
        result = await store.confirm_resolution(incident_id, body.operator.strip(), body.fixed)
    except LookupError:
        raise HTTPException(404, "incident not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    result["incident"] = await _broadcast(request, incident_id)
    return result


@router.get("/learning/resolutions")
async def resolutions(store=Depends(get_store)):
    """Everything the learning runbook has recorded so far."""
    return await store.all_resolutions()
