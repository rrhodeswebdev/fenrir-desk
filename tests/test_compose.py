"""Static checks on the Docker setup (18.2, DEC-1, DEC-6): the app holds a shell, so the
port stays on host loopback and nothing the terminal can write reaches the host's next build."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_TEXT = (ROOT / "compose.yaml").read_text()
SVC = yaml.safe_load(COMPOSE_TEXT)["services"]["fenrir"]
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_ports_published_on_host_loopback_only():
    assert SVC["ports"], "no published ports"
    for port in SVC["ports"]:
        assert str(port).startswith(("127.0.0.1:", "[::1]:")), f"{port} reaches the network"
    assert {str(p).rsplit(":", 2)[0] for p in SVC["ports"]} == {"127.0.0.1", "[::1]"}


def test_build_and_git_files_mounted_read_only_over_the_repo():
    volumes = SVC["volumes"]
    assert ".:/app" in volumes
    for path in ("compose.yaml", "Dockerfile", ".dockerignore", ".git/config", ".git/hooks"):
        assert f"./{path}:/app/{path}:ro" in volumes


def test_named_volumes_for_secrets_and_claude_login():
    assert "fenrir-private:/var/lib/fenrir" in SVC["volumes"]
    assert "claude-config:/home/claude/.claude-config" in SVC["volumes"]


def test_settings_never_come_from_compose():
    # os.environ overrides the secrets file, so compose must not set or pass any FENRIR_* value
    assert "env_file" not in SVC and "environment" not in SVC
    assert not re.search(r"\$\{?FENRIR_", COMPOSE_TEXT)


def test_hardening_and_restart():
    assert "no-new-privileges:true" in SVC["security_opt"]
    assert SVC["restart"] == "unless-stopped"
    assert not SVC.get("privileged") and "docker.sock" not in COMPOSE_TEXT


def test_base_images_pinned():
    images = re.findall(r"^FROM\s+(\S+)|--from=(\S+/\S+)", DOCKERFILE, re.M)
    refs = [a or b for a, b in images]
    assert len(refs) == 3
    for ref in refs:
        assert re.search(r":\d+\.\d+\.\d+", ref), f"{ref} is not pinned to a version"


def test_web_build_gets_brand_assets():
    assert re.search(r"^COPY assets/brand/ assets/brand/$", DOCKERFILE, re.M)
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert "!assets/brand" in ignore


def test_image_isolation_env():
    for line in (
        "FENRIR_BIND_ALL=1",
        "FENRIR_PTY_USER=claude",
        "FENRIR_PRIVATE_DIR=/var/lib/fenrir",
        "FENRIR_ENV_FILE=/var/lib/fenrir/.env",
    ):
        assert line in DOCKERFILE
    assert "install -d -m 0700 /var/lib/fenrir" in DOCKERFILE
