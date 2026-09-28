from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

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


@router.get("/incidents")
async def incidents(limit: int = Query(50, ge=1, le=500), store=Depends(get_store)):
    return await store.list_incidents(limit)
