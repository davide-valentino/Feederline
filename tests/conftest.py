import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

# Must be set before app.config is imported: the operator boundary fails closed when
# OPERATOR_TOKEN is not configured.
os.environ.setdefault("OPERATOR_TOKEN", "test-operator-token")

from app.config import settings
from app.database import Base, make_engine, get_session
from app.main import app


@pytest.fixture
def test_session_factory():
    test_engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(test_engine)
    test_session_factory = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False
    )
    yield test_session_factory
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def operator_headers():
    from app.config import settings

    return {"X-Operator-Token": settings.operator_token}


# Legacy execution routes are operator-only. Tests that exercise them through the
# normal client get the token injected so the fixture stays focused on behaviour;
# the boundary itself is covered explicitly with ``raw_client``.
_LEGACY_PREFIXES = (
    "/v1/sources",
    "/v1/destinations",
    "/v1/stream-profiles",
    "/v1/deliveries",
)


class OperatorAwareClient:
    def __init__(self, client):
        self._client = client

    def _headers(self, url, headers):
        if url.startswith(_LEGACY_PREFIXES):
            merged = {"X-Operator-Token": settings.operator_token}
            if headers:
                merged.update(headers)
            return merged
        return headers

    def _call(self, method, url, **kwargs):
        kwargs["headers"] = self._headers(url, kwargs.get("headers"))
        return getattr(self._client, method)(url, **kwargs)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._call("put", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._call("patch", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("delete", url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def raw_client(test_session_factory):

    def override_session():
        with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def client(raw_client):
    return OperatorAwareClient(raw_client)
