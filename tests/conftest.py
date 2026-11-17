import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from token_router.db.session import Base, get_db
from token_router.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

shared_test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False,
)
shared_session_factory = async_sessionmaker(shared_test_engine, expire_on_commit=False, class_=AsyncSession)


async def override_get_db():
    async with shared_session_factory() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db():
    async with shared_test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with shared_test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
