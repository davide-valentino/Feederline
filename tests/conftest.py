import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

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
def client(test_session_factory):

    def override_session():
        with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
