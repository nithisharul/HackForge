import json

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_pipeline
from app.config import PROJECT_DIR

router = APIRouter(tags=["models"])
RESULTS = PROJECT_DIR / "ml_research" / "benchmark_results.json"


@router.get("/benchmark")
async def benchmark():
    """Model comparison table produced by scripts/run_benchmark.py."""
    if not RESULTS.exists():
        raise HTTPException(404, "No benchmark yet. Run: python scripts/run_benchmark.py")
    return json.loads(RESULTS.read_text())


@router.get("/models")
async def models(pipeline=Depends(get_pipeline)):
    b = pipeline.bundle
    return {"loaded": list(b.models), "calibration": {k: v.__dict__ for k, v in b.calibrations.items()},
            "metadata": b.metadata}


@router.post("/models/reload")
async def reload_models(pipeline=Depends(get_pipeline)):
    """Hot-swap to freshly trained artifacts without restarting. Also clears any
    failed-model state, so this is the recovery step for degraded mode."""
    return {"loaded": pipeline.reload_models(), "model_health": pipeline.detector.model_health()}
