from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from daylog.vault import Vault


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def vault_repo(tmp_path: Path) -> Path:
    """A temp directory that is a real git repo, standing in for the vault."""
    repo = tmp_path / "vault"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    # An empty repo has no commits, so `git push` has nothing to complain
    # about being on the wrong branch; give it one so `_commit` has history
    # to build on, matching a real vault clone.
    (repo / ".gitkeep").write_text("")
    _git("add", ".gitkeep", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def vault(vault_repo: Path) -> Vault:
    return Vault(vault_repo)


@pytest.fixture
def vault_with_remote(tmp_path: Path) -> Iterator[tuple[Vault, Path]]:
    """A vault repo with a bare remote configured, so pushes can succeed."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)

    repo = tmp_path / "vault"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    (repo / ".gitkeep").write_text("")
    _git("add", ".gitkeep", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)

    yield Vault(repo), remote
