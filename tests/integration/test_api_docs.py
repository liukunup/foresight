"""Tests for the generated API documentation endpoints."""

from __future__ import annotations


async def test_openapi_json_describes_the_api(api_harness):
    """The machine-readable schema is served with the application title."""
    harness = await api_harness()
    response = await harness.client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "ForeSight API"
    assert "/api/health" in schema["paths"]
    assert "/api/messages" in schema["paths"]


async def test_swagger_ui_is_served(api_harness):
    """The interactive Swagger UI is reachable at /docs."""
    harness = await api_harness()
    response = await harness.client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()


async def test_redoc_is_served(api_harness):
    """The ReDoc view is reachable at /redoc."""
    harness = await api_harness()
    response = await harness.client.get("/redoc")
    assert response.status_code == 200
    assert "redoc" in response.text.lower()


async def test_documentation_does_not_need_the_api_token(api_harness):
    """The docs live outside /api, so they are not bearer-protected."""
    harness = await api_harness(token="secret-token")
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = await harness.client.get(path)
        assert response.status_code == 200, path
