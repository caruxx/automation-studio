from __future__ import annotations

import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-with-at-least-32-characters"
os.environ["MFA_REQUIRED"] = "false"
os.environ["COOKIE_SECURE"] = "false"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["AUTH_LOGIN_IP_MAX_ATTEMPTS"] = "100"
os.environ["AUTH_LOGIN_EMAIL_MAX_ATTEMPTS"] = "100"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models.db_models import User
from app.rate_limit import clear_rate_limits
from app.security import hash_password


TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def reset_database():
    clear_rate_limits()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def create_user():
    def factory(
        *,
        email: str,
        password: str = "Valid!Pass9072",
        role: str = "user",
        totp_secret: str | None = None,
    ) -> User:
        with TestingSessionLocal() as db:
            user = User(
                email=email,
                password_hash=hash_password(password),
                name=email.split("@", 1)[0],
                role=role,
                is_active=True,
                totp_secret=totp_secret,
                totp_enabled=totp_secret is not None,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.expunge(user)
            return user

    return factory
