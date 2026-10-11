from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fenrir.server.app import create_app, static_file
from fenrir.settings import Settings

KEY = "k" * 43
CODE = "c" * 22
INDEX = "<html><head></head><body>app</body></html>"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX)
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    (static / "link.txt").symlink_to(tmp_path / "secret.txt")
    return static


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(Settings(), KEY, CODE, root), base_url="http://localhost:8000")


def test_static_file_resolves_inside_root_only(root: Path):
    real = root.resolve()
    assert static_file(real, "assets/app.js") == real / "assets" / "app.js"
    for path in ["", "assets", "missing.js", "../secret.txt", "/etc/passwd", "link.txt", "a\x00b"]:
        assert static_file(real, path) is None


def test_index_served_without_secrets(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert r.text == INDEX
    assert KEY not in r.text and CODE not in r.text


def test_deep_link_returns_index(client: TestClient):
    r = client.get("/backtests/abc")
    assert r.status_code == 200
    assert r.text == INDEX


def test_static_file_served(client: TestClient):
    assert client.get("/assets/app.js").text == "console.log(1)"


@pytest.mark.parametrize(
    "path",
    [
        "/..%2fsecret.txt",
        "/..%2f..%2fsecret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/link.txt",
        "/a%00b",
    ],
)
def test_static_does_not_escape_dir(client: TestClient, path: str):
    r = client.get(path)
    assert "do not serve" not in r.text


@pytest.mark.parametrize("path", ["/api", "/api/nope", "/ws", "/ws/nope"])
def test_api_and_ws_paths_never_fall_back(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code == 404
    assert "app" not in r.text


def test_health_still_wins_over_spa(client: TestClient):
    assert client.get("/api/health").json() == {"ok": True}


def test_no_spa_without_static_dir():
    client = TestClient(create_app(Settings(), KEY, CODE, None), base_url="http://localhost:8000")
    assert client.get("/").status_code == 404
    assert client.get("/backtests/abc").status_code == 404
