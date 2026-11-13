"""Tests for the free-form read-only SQL endpoint.

The endpoint is the security boundary of the API: it runs a statement
the caller wrote, so the tests check both that a read works and that the
ways of writing to the database are refused.
"""

from __future__ import annotations


async def test_query_returns_rows(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT message_id, content FROM messages ORDER BY message_id",
            "params": {},
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 4
    assert body["columns"] == ["message_id", "content"]


async def test_query_supports_named_params(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT COUNT(*) AS c FROM messages WHERE channel_id = :channel",
            "params": {"channel": 20},
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[4]]


async def test_query_rejects_insert(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "INSERT INTO messages (message_id) VALUES (1)", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_drop(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_multi_statement(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT 1; DROP TABLE messages", "params": {}},
    )
    assert response.status_code == 400


async def test_query_rejects_write_cte(api_client, auth):
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "WITH x AS (DELETE FROM messages RETURNING message_id) SELECT * FROM x",
            "params": {},
        },
    )
    assert response.status_code == 400


async def test_the_query_endpoint_requires_a_token(api_client):
    """The protection lives at the mount site, not on the routes."""
    response = await api_client.post("/api/query", json={"sql": "SELECT 1"})
    assert response.status_code == 401


async def test_query_leaves_the_data_unchanged(api_client, auth):
    """A refused statement changes nothing.

    The endpoint is the only way into the dump, so the number of the
    messages after a refused write is the proof that the write did not
    happen.
    """
    rejected = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "INSERT INTO messages (message_id) VALUES (99)", "params": {}},
    )
    assert rejected.status_code == 400
    counted = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT COUNT(*) FROM messages", "params": {}},
    )
    assert counted.status_code == 200
    assert counted.json()["rows"] == [[4]]


async def test_query_clamps_the_limit(api_client, auth):
    """The limit is clamped, not refused.

    A caller that asks for more rows than the maximum gets the maximum,
    and a caller that asks for none gets one row: it asked for a page,
    not for a refusal.
    """
    statement = {
        "sql": "SELECT message_id FROM messages ORDER BY message_id",
        "params": {},
    }
    too_small = await api_client.post(
        "/api/query", headers=auth, json={**statement, "limit": 0}
    )
    assert too_small.status_code == 200
    assert too_small.json()["row_count"] == 1
    too_large = await api_client.post(
        "/api/query", headers=auth, json={**statement, "limit": 10_000}
    )
    assert too_large.status_code == 200
    assert too_large.json()["row_count"] == 4


async def test_query_pages_with_the_offset(api_client, auth):
    """The offset skips the rows of the previous page."""
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": "SELECT message_id FROM messages ORDER BY message_id",
            "params": {},
            "limit": 2,
            "offset": 2,
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [[3], [4]]


async def test_query_is_denied_by_the_engine(api_client, auth):
    """The engine refuses a statement the validation let through.

    The validation is the first layer and it tells the caller what it
    did not like. The authorizer in the engine is the layer that cannot
    be talked out of: it allows a fixed set of actions, and a statement
    that asks for another one is denied there, in the database.
    """
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={
            "sql": (
                "WITH RECURSIVE x(n) AS ("
                "SELECT 1 UNION ALL SELECT n + 1 FROM x WHERE n < 5"
                ") SELECT * FROM x"
            ),
            "params": {},
        },
    )
    assert response.status_code == 400


async def test_query_reports_a_broken_statement(api_client, auth):
    """A statement the database cannot parse is a bad request too.

    The caller wrote the statement, so the caller is the one to fix it:
    the answer is a 400 and not a failure of the server.
    """
    response = await api_client.post(
        "/api/query",
        headers=auth,
        json={"sql": "SELECT FROM WHERE", "params": {}},
    )
    assert response.status_code == 400
