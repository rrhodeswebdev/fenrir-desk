import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    """Async tests use anyio's pytest plugin (`pytest.mark.anyio`) on asyncio; no other plugin."""
    return "asyncio"
