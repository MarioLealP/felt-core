"""GET /v1/metrics/latency."""

from fastapi import APIRouter

from felt_core.config import load_config
from felt_core.core import metrics
from felt_core.db.latency import get_latency_rows

router = APIRouter()


@router.get("/v1/metrics/latency")
async def latency_metrics_endpoint(since_hours: int = 24, endpoint: str | None = None) -> dict:
    """Return latency stats computed from the recorded request rows."""
    config = load_config()
    rows = get_latency_rows(config.db_path, since_hours=since_hours, endpoint=endpoint)
    return metrics.compute_stats(rows)
