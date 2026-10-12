import os

import pytest

from fenrir.files import read_regular


def test_reads_a_regular_file_up_to_the_limit(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"12345")
    assert read_regular(path, 5) == b"12345"
    with pytest.raises(ValueError, match=f"{path} is over 4 bytes"):
        read_regular(path, 4)


def test_reads_a_file_spanning_several_chunks_exactly_to_the_limit(tmp_path):
    data = os.urandom((3 << 20) + 7)  # 3 MiB + 7 bytes: four 1 MiB reads
    (path := tmp_path / "f").write_bytes(data)
    assert read_regular(path, len(data)) == data
    with pytest.raises(ValueError, match="over"):
        read_regular(path, len(data) - 1)


@pytest.mark.parametrize(
    ("plant", "message"),
    [
        (lambda p, secret: p.symlink_to(secret), "is a symlink"),
        (lambda p, secret: os.mkfifo(p), "is not a regular file"),  # a blocking open would hang
        (lambda p, secret: p.mkdir(), "is not a regular file"),
    ],
    ids=["symlink", "fifo", "directory"],
)
def test_refuses_a_symlink_fifo_or_directory_naming_it_without_leaking_fds(
    tmp_path, plant, message
):
    secret = tmp_path / "secret"
    secret.write_text("TOKEN=secret-0001")
    path = tmp_path / "f"
    plant(path, secret)
    before = len(os.listdir("/dev/fd"))
    for _ in range(50):
        with pytest.raises(ValueError, match=f"{path} {message}") as e:
            read_regular(path, 1 << 20)
        assert "secret-0001" not in str(e.value)
    assert len(os.listdir("/dev/fd")) == before


def test_over_the_limit_leaks_no_fd(tmp_path):
    (path := tmp_path / "f").write_bytes(b"x" * 10)
    before = len(os.listdir("/dev/fd"))
    for _ in range(50):
        with pytest.raises(ValueError, match="over"):
            read_regular(path, 9)
    assert len(os.listdir("/dev/fd")) == before


def test_missing_and_unreadable_files_keep_their_os_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_regular(tmp_path / "missing", 10)
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    (path := tmp_path / "f").write_text("x")
    path.chmod(0)
    with pytest.raises(PermissionError):
        read_regular(path, 10)
