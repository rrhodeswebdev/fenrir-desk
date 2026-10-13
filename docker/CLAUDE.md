# docker/

Files `compose.yaml` mounts into the container. Not in the image's build context.

- `empty`: a committed empty file mounted read-only over `/app/.env` and the four compose override names, which the host's `docker compose` reads from the project directory (#185). Keep it empty.
- Not here: the `Dockerfile`, `compose.yaml` and `.dockerignore` (repo root), the image's isolation (`src/fenrir/server/isolation.py`).
- Tests: `tests/test_compose.py`; in the running image, `tests/test_image_isolation.py`.
