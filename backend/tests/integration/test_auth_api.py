import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.main import create_app
from app.models import Base

TEST_DB = "sqlite+pysqlite:///:memory:"


@pytest.fixture
def client():
    engine = create_engine(
        TEST_DB,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTest = sessionmaker(bind=engine)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: SessionTest()
    yield TestClient(app)
    Base.metadata.drop_all(engine)


def test_register_creates_user_and_profile(client):
    r = client.post("/auth/register", json={"email": "a@b.com", "password": "pass1234"})
    assert r.status_code == 201
    data = r.json()
    assert data["email"] == "a@b.com"
    assert data["role"] == "user"


def test_register_duplicate_email_fails(client):
    client.post("/auth/register", json={"email": "dup@b.com", "password": "pass1234"})
    r = client.post("/auth/register", json={"email": "dup@b.com", "password": "pass1234"})
    assert r.status_code == 409


def test_login_returns_token(client):
    client.post("/auth/register", json={"email": "u@b.com", "password": "mypassword"})
    r = client.post("/auth/login", json={"email": "u@b.com", "password": "mypassword"})
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_login_wrong_password_fails(client):
    client.post("/auth/register", json={"email": "u2@b.com", "password": "correct"})
    r = client.post("/auth/login", json={"email": "u2@b.com", "password": "wrong"})
    assert r.status_code == 401


def test_me_returns_current_user(client):
    client.post("/auth/register", json={"email": "me@b.com", "password": "pass1234"})
    login = client.post("/auth/login", json={"email": "me@b.com", "password": "pass1234"})
    token = login.json()["access_token"]
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "me@b.com"


def test_me_without_token_returns_401(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
