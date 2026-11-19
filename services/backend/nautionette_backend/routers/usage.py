"""Session-only, metric-only usage analytics with private member scopes."""

from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from .. import usage
from ..security import Principal, require_user

router = APIRouter(prefix="/api/settings/usage", dependencies=[Depends(require_user)])


def selection(
    who: Annotated[Principal, Depends(require_user)],
    start: str | None = None,
    end: str | None = None,
    timezone: str = "UTC",
    group: str = "models",
    user: str = "",
    model: str = "",
    agent: str = "",
    workflow: str = "",
    workload: str = "",
    sort: str = "tokens",
) -> dict:
    try:
        today = datetime.now(ZoneInfo(timezone)).date()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, "Choose a supported timezone") from exc
    return dict(
        who=who,
        start=start or (today - timedelta(days=29)).isoformat(),
        end=end or today.isoformat(),
        timezone=timezone,
        group=group,
        user=user,
        model=model,
        agent=agent,
        workflow=workflow,
        workload=workload,
        sort=sort,
    )


@router.get("")
def get_usage(
    selected: Annotated[dict, Depends(selection)],
    response: Response,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    chart_group: str | None = None,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return usage.report(**selected, offset=offset, limit=limit, chart_group=chart_group)


@router.get("/export")
def export_usage(selected: Annotated[dict, Depends(selection)]) -> Response:
    # Explicit cap, never silently truncate an export to the UI's first page.
    data = usage.report(**selected, limit=10001)
    if data["count"] > 10000:
        raise HTTPException(422, "This export exceeds 10,000 rows. Choose a narrower date range or filter")
    return Response(
        "\ufeff" + usage.export_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="denkzeug-usage.csv"',
            "Cache-Control": "no-store",
        },
    )
