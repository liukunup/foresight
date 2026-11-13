async def test_health_requires_token(api_client):
    response = await api_client.get("/api/health")
    assert response.status_code == 401


async def test_health_returns_counts(api_client, auth):
    response = await api_client.get("/api/health", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["total_messages"] == 4


async def test_messages_are_paginated(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 2
    assert body["has_more"] is True
    assert len(body["items"]) == 2


async def test_messages_limit_is_capped(api_client, auth):
    response = await api_client.get("/api/messages", headers=auth, params={"limit": 5000})
    assert response.status_code == 200
    assert response.json()["limit"] == 500


async def test_messages_search_is_case_insensitive(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"q": "MESSAGE NUMBER 1"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_messages_filter_by_channel(api_client, auth):
    response = await api_client.get(
        "/api/messages", headers=auth, params={"channel_id": 999}
    )
    assert response.json()["total"] == 0


async def test_message_detail(api_client, auth):
    response = await api_client.get("/api/messages/2", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == 2
    assert body["analysis"] is None


async def test_message_detail_not_found(api_client, auth):
    response = await api_client.get("/api/messages/9999", headers=auth)
    assert response.status_code == 404
