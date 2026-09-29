"""Demo helpers: inject an anomaly on demand ("watch me break it")."""
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_pipeline
from app.config import settings
from app.sim.simulator import SCENARIOS

router = APIRouter(prefix="/demo", tags=["demo"])


class InjectRequest(BaseModel):
    kind: str = Field("error_spike", description=" | ".join(SCENARIOS))
    duration_seconds: float = Field(120, ge=10, le=1800)


@router.get("/scenarios")
async def scenarios():
    return SCENARIOS


@router.post("/inject-anomaly")
async def inject(req: InjectRequest, pipeline=Depends(get_pipeline)):
    if not settings.enable_demo_endpoints:
        raise HTTPException(403, "demo endpoints disabled")
    if req.kind not in SCENARIOS:
        raise HTTPException(400, f"unknown scenario; options: {list(SCENARIOS)}")
    now = time.time()
    if pipeline.simulator is not None:
        pipeline.simulator.inject(req.kind, now, req.duration_seconds)
        via = "built-in simulator"
    else:
        # hand the request to scripts/generate_logs.py, which polls this file
        settings.inject_control_file.parent.mkdir(parents=True, exist_ok=True)
        settings.inject_control_file.write_text(json.dumps(
            {"kind": req.kind, "duration_seconds": req.duration_seconds, "requested_at": now}))
        via = f"control file {settings.inject_control_file.name} (picked up by generate_logs.py)"
    return {"ok": True, "kind": req.kind, "description": SCENARIOS[req.kind],
            "duration_seconds": req.duration_seconds, "via": via}


class BreakModel(BaseModel):
    name: str = Field("lstm_ae", description="iforest | lstm_ae")


@router.post("/break-model")
async def break_model(req: BreakModel, pipeline=Depends(get_pipeline)):
    """Simulate a detector crashing, to show degraded mode. The other detectors
    keep working. Recover with POST /models/reload."""
    if not settings.enable_demo_endpoints:
        raise HTTPException(403, "demo endpoints disabled")
    if req.name not in pipeline.bundle.models:
        raise HTTPException(400, f"model not loaded; loaded: {list(pipeline.bundle.models)}")
    pipeline.detector.break_model(req.name)
    return {"ok": True, "broken": req.name,
            "recover_with": "POST /models/reload"}
