"""Tests for the bearer authentication of the API.

The token comes from `FORESIGHT_API_TOKEN`: unset means the API is open
(the localhost mode), set means every route under `/api` demands a
matching bearer token.
"""

from __future__ import annotations

from fastapi import APIRouter

from foresight.api.app import include_api_router


async def test_missing_token_is_rejected(api_client):
    response = await api_client.get("/api/health")
    assert response.status_code == 401


async def test_wrong_token_is_rejected(api_client):
    """A token that does not match is worth exactly as much as no token."""
    response = await api_client.get(
        "/api/health", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


async def test_non_bearer_scheme_is_rejected(api_client):
    """The scheme is part of the credential: `Basic secret-token` is not one."""
    response = await api_client.get(
        "/api/health", headers={"Authorization": "Basic secret-token"}
    )
    assert response.status_code == 401


async def test_bearer_scheme_is_case_insensitive(api_client, auth):
    """RFC 7235 §2.1: the authentication scheme is case insensitive."""
    response = await api_client.get(
        "/api/health", headers={"Authorization": "bearer secret-token"}
    )
    assert response.status_code == 200


async def test_unauthorized_response_carries_www_authenticate(api_client):
    """RFC 7235 §3.1: a 401 names the scheme the client should use."""
    response = await api_client.get("/api/health")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_whitespace_token_still_requires_a_bearer_header(api_client, monkeypatch):
    """A token of blanks is a token, not an unset variable.

    Stripping it would silently open the API on a deployment that meant
    to protect it.
    """
    monkeypatch.setenv("FORESIGHT_API_TOKEN", "   ")
    response = await api_client.get("/api/health")
    assert response.status_code == 401


async def test_mounted_router_is_protected_without_its_own_dependency(api_harness):
    """The protection lives at the mount site, not on the routes.

    A router added later must not be able to forget the dependency: the
    mount site applies it to every route of the router.
    """
    harness = await api_harness()
    router = APIRouter()

    @router.get("/probe")
    async def probe():
        return {"ok": True}

    include_api_router(harness.app, router, tags=["probe"])

    response = await harness.client.get("/api/probe")
    assert response.status_code == 401
    response = await harness.client.get(
        "/api/probe", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 200
