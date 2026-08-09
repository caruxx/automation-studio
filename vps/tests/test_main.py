from fastapi.testclient import TestClient

from app.main import create_app


def test_docs_are_disabled_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    with TestClient(create_app()) as client:
        response = client.get("/docs")

    assert response.status_code == 404
