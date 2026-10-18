"""Exercise deletion guards and local recovery archives without GitHub or user files."""

import importlib.util
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cleanup_merged", Path(__file__).resolve().parents[1] / "scripts/cleanup_merged.py"
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def git(root, *args):
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "primary"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Cleanup Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "base").write_text("initial")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    git(root, "checkout", "-b", "feature/example")
    (root / "change").write_text("integrated")
    git(root, "add", ".")
    git(root, "commit", "-m", "feature")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "main")
    git(root, "merge", "--squash", "feature/example")
    git(root, "commit", "-m", "squash (#1)")
    merged = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", merged)
    prs = [
        {
            "headRefName": "feature/example",
            "headRefOid": head,
            "state": "MERGED",
            "mergeCommit": {"oid": merged},
            "isCrossRepository": False,
        }
    ]
    return root, head, prs


def test_squash_requires_merged_pr_evidence(repository):
    root, head, prs = repository
    assert not cleanup.ancestor(root, head, "origin/main")
    assert cleanup.eligible(root, "feature/example", head, prs, "main")
    assert not cleanup.eligible(root, "feature/example", head, [], "main")
    assert not cleanup.eligible(root, "main", head, prs, "main")
    assert not cleanup.eligible(
        root, "feature/example", head, [{**prs[0], "state": "CLOSED"}], "main"
    )
    assert not cleanup.eligible(
        root, "feature/example", head, [*prs, {**prs[0], "state": "OPEN"}], "main"
    )


def test_new_same_tree_commit_is_not_treated_as_merged(repository):
    root, _head, prs = repository
    git(root, "checkout", "feature/example")
    git(root, "commit", "--allow-empty", "-m", "new unmerged work")
    newer = git(root, "rev-parse", "HEAD")
    assert not cleanup.eligible(root, "feature/example", newer, prs, "main")
    assert not cleanup.eligible(root, "feature/example", newer, prs, "main", equivalent_tree=False)


def test_local_rebased_tree_can_match_but_remote_requires_exact_head(repository):
    root, head, prs = repository
    tree = git(root, "rev-parse", head + "^{tree}")
    timestamp = git(root, "show", "-s", "--format=%cI", head)
    alternate = subprocess.check_output(
        ["git", "commit-tree", tree, "-m", "pre-rebase equivalent"],
        cwd=root,
        text=True,
        env={**os.environ, "GIT_COMMITTER_DATE": timestamp},
    ).strip()
    assert cleanup.eligible(root, "feature/example", alternate, prs, "main")
    assert not cleanup.eligible(
        root, "feature/example", alternate, prs, "main", equivalent_tree=False
    )


def test_primary_dirty_locked_and_active_worktrees_are_kept(repository, tmp_path):
    root, head, _ = repository
    path = tmp_path / "feature"
    git(root, "worktree", "add", str(path), "feature/example")
    entry = {"worktree": str(path), "HEAD": head, "branch": "refs/heads/feature/example"}
    assert cleanup.removable(root, entry, head, [])
    assert not cleanup.removable(root, {**entry, "locked": "reason"}, head, [])
    assert not cleanup.removable(root, entry, head, [path / "nested"])
    assert not cleanup.removable(root, {"worktree": str(root)}, git(root, "rev-parse", "HEAD"), [])
    (path / "untracked").write_text("keep")
    assert not cleanup.removable(root, entry, head, [])


def test_backup_keeps_local_data_and_skips_rebuildable_files(repository, tmp_path):
    root, _, _ = repository
    (root / ".gitignore").write_text(".env\n.local/\n.venv/\n")
    (root / ".env").write_text("FAKE_TEST_KEY=not-a-secret\n")
    (root / ".local").mkdir()
    (root / ".local/demo.sqlite3").write_bytes(b"synthetic database")
    (root / ".venv").mkdir()
    (root / ".venv/rebuildable").write_text("cache")
    target = tmp_path / "backup.tar.gz"
    cleanup.archive_data(root, target)
    with tarfile.open(target) as archive:
        assert set(archive.getnames()) == {".env", ".local/demo.sqlite3"}
        assert archive.extractfile(".local/demo.sqlite3").read() == b"synthetic database"
    assert target.stat().st_mode & 0o777 == 0o600


def test_same_commit_branch_switch_is_kept(repository, tmp_path):
    root, head, _ = repository
    path = tmp_path / "feature"
    git(root, "worktree", "add", str(path), "feature/example")
    entry = {"worktree": str(path), "branch": "refs/heads/feature/example"}
    git(path, "checkout", "-b", "feature/new-work")
    assert not cleanup.removable(root, entry, head, [])


def test_open_file_outside_process_cwd_keeps_worktree(repository, tmp_path):
    root, head, _ = repository
    path = tmp_path / "feature"
    git(root, "worktree", "add", str(path), "feature/example")
    entry = {"worktree": str(path), "branch": "refs/heads/feature/example"}
    assert not cleanup.removable(root, entry, head, [path / ".local" / "database.sqlite3"])
