from fastapi import APIRouter, Depends, Query

from app.api.deps import get_pipeline

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def latest(pipeline=Depends(get_pipeline)):
    """Most recent tick: rolling error rate, baseline, z-score, detector scores."""
    return pipeline.metrics_history[-1] if pipeline.metrics_history else None


@router.get("/metrics/history")
async def history(minutes: float = Query(15, gt=0, le=60), pipeline=Depends(get_pipeline)):
    n = int(minutes * 60 / pipeline.cfg.tick_seconds)
    return list(pipeline.metrics_history)[-n:]
