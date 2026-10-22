from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from bayaan_server.api.app import create_app
from bayaan_server.config import Settings

def test_admin_auth_unauthorized():
    """Requests to /admin endpoints return 401 without credentials."""
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    
    response = client.get("/admin")
    assert response.status_code in [401, 403]

def test_admin_auth_authorized():
    """Valid admin credentials grant access to /admin."""
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    
    # Basic Auth header: admin:admin -> base64(admin:admin) = YWRtaW46YWRtaW4=
    headers = {"Authorization": "Basic YWRtaW46YWRtaW4="}
    response = client.get("/admin", headers=headers)
    
    assert response.status_code == 200

def test_admin_auth_invalid_credentials():
    """Invalid credentials return 403."""
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    
    headers = {"Authorization": "Basic d3JvbmcgYXV0aDpwYXNzd29yZDEyMw=="} # wrong:password123
    response = client.get("/admin", headers=headers)
    assert response.status_code == 403

def test_api_route_unauthorized():
    """Requests to administrative API routes return 401 without credentials."""
    settings = Settings()
    app = create_app(settings)
    client = TestClient(app)
    
    # Test a few admin API routes
    routes_to_test = [
        "/api/v1/diagnostics/system-summary",
        "/api/v1/glossary/terms",
        "/api/v1/sermons",
        "/api/v1/scripture",
        "/api/v1/sessions/active",
    ]
    
    for route in routes_to_test:
        response = client.get(route)
        assert response.status_code in [401, 403], f"Route {route} should be protected"

def test_api_route_authorized():
    """Valid admin credentials grant access to administrative API routes."""
    settings = Settings()
    app = create_app(settings)
    # Manually trigger lifespan to populate app.state.service
    with TestClient(app) as client:
        headers = {"Authorization": "Basic YWRtaW46YWRtaW4="}
        routes_to_test = [
            "/api/v1/diagnostics/system-summary",
            "/api/v1/glossary/terms",
            "/api/v1/sermons",
            "/api/v1/scripture",
            "/api/v1/sessions/active",
        ]
        
        for route in routes_to_test:
            response = client.get(route, headers=headers)
            assert response.status_code == 200, f"Route {route} should be accessible with admin creds"


def test_operator_token_login_and_bearer_auth():
    """Operator can login via POST /api/v1/auth/login and authenticate with Bearer token."""
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as client:
        # Invalid login
        bad_res = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrongpassword"})
        assert bad_res.status_code == 401

        # Valid login
        res = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        token = data["access_token"]

        # Call /me with Bearer token
        me_res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_res.status_code == 200
        assert me_res.json()["username"] == "admin"

        # Access admin API with Bearer token
        diag_res = client.get("/api/v1/diagnostics/system-summary", headers={"Authorization": f"Bearer {token}"})
        assert diag_res.status_code == 200
