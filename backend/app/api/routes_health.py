from fastapi import APIRouter, Depends

from app.api.deps import get_dispatcher, get_pipeline
from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(pipeline=Depends(get_pipeline), dispatcher=Depends(get_dispatcher)):
    return {
        "status": "ok",
        "pipeline": pipeline.status(),
        "publishers": dispatcher.status(),
        "aws_enabled": settings.aws_enabled,
        "config": {
            "window_seconds": settings.window_seconds,
            "tick_seconds": settings.tick_seconds,
            "baseline_warmup_ticks": settings.baseline_warmup_ticks,
            "severity_thresholds": {"LOW": settings.sev_low, "MEDIUM": settings.sev_medium,
                                    "HIGH": settings.sev_high, "CRITICAL": settings.sev_critical},
            "critical_error_rate": settings.critical_error_rate,
        },
    }
