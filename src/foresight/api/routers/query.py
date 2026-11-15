"""Free-form read-only SQL endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from foresight.api.query import (
    QueryDatabaseError,
    QueryError,
    QueryResult,
    clamp_limit,
)

router = APIRouter()


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int | None = None
    offset: int = Field(default=0, ge=0)


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: float


@router.post("/query", response_model=QueryResponse)
async def run_query(payload: QueryRequest, request: Request) -> QueryResponse:
    """Run a read-only statement and return its rows.

    The statement is not built here: the runner in `foresight.api.query`
    owns the SQL and the rules that keep it read-only. A statement the
    runner refuses is a bad request — the caller sent something that is
    not a read — and not a failure of the server. A database that
    cannot answer at all is the opposite: there is nothing the caller
    can fix, so it is a failure of the server.
    """
    runner = request.app.state.query_runner
    try:
        result: QueryResult = await runner.run(
            payload.sql, payload.params, clamp_limit(payload.limit), payload.offset
        )
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except QueryDatabaseError as exc:
        raise HTTPException(
            status_code=500, detail="the database could not be read"
        ) from exc
    return QueryResponse(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
    )
