"""Audit or remove merged feature branches/worktrees, preserving recoverable local data."""

import argparse
import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

REBUILDABLE = {".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "dist", "build"}


def run(*args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def git(root, *args):
    return run("git", *args, cwd=root).stdout.strip()


def primary_root():
    return Path(git(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def worktrees(root):
    result = []
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        entry = dict(
            line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines()
        )
        result.append(entry)
    return result


def ancestor(root, older, newer):
    return (
        run("git", "merge-base", "--is-ancestor", older, newer, cwd=root, check=False).returncode
        == 0
    )


def eligible(root, branch, head, prs, default, *, equivalent_tree=True):
    """Squash/rebased stacks need merged PR evidence, not just branch ancestry."""
    if not branch.startswith("feature/") or branch == default:
        return False
    matching = [pr for pr in prs if pr["headRefName"] == branch and not pr["isCrossRepository"]]
    if any(pr["state"] == "OPEN" for pr in matching):
        return False
    if ancestor(root, head, f"origin/{default}"):
        return True
    for pr in matching:
        if pr["state"] != "MERGED" or not pr.get("mergeCommit"):
            continue
        if not ancestor(root, pr["mergeCommit"]["oid"], f"origin/{default}"):
            continue
        if head == pr["headRefOid"]:
            return True
        if not equivalent_tree or ancestor(root, pr["headRefOid"], head):
            continue
        # Reject new local commits made after the merged PR head was committed.
        if int(git(root, "show", "-s", "--format=%ct", head)) > int(
            git(root, "show", "-s", "--format=%ct", pr["headRefOid"])
        ):
            continue
        # A local pre-rebase head is safe only when its complete tree is identical.
        result = run("git", "diff", "--quiet", head, pr["headRefOid"], cwd=root, check=False)
        if result.returncode == 0:
            return True
    return False


def preserved_files(path):
    raw = run(
        "git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=path
    ).stdout
    return [name for name in raw.split("\0") if name and not (set(Path(name).parts) & REBUILDABLE)]


def archive_data(path, destination):
    names = preserved_files(path)
    if not names:
        return
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for name in names:
            archive.add(path / name, arcname=name, recursive=False)
    os.chmod(destination, 0o600)
    # Verify file contents without logging or exposing secrets.
    with tarfile.open(destination, "r:gz") as archive:
        for name in names:
            source = path / name
            if source.is_file() and not source.is_symlink():
                with source.open("rb") as original, archive.extractfile(name) as saved:
                    while chunk := original.read(1024 * 1024):
                        if saved.read(len(chunk)) != chunk:
                            raise RuntimeError("Local backup verification failed")
                    if saved.read(1):
                        raise RuntimeError("Local backup verification failed")


def busy_paths():
    """Keep worktrees with a working directory or open file in a visible process."""
    result = run("lsof", "-Fn", cwd=Path.cwd(), check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError("Cannot inspect process working directories")
    return [Path(line[1:]) for line in result.stdout.splitlines() if line.startswith("n/")]


def removable(root, entry, head, busy):
    path = Path(entry["worktree"])
    if path.resolve() == root.resolve() or "locked" in entry or "prunable" in entry:
        return False
    if any(location == path or path in location.parents for location in [Path.cwd(), *busy]):
        return False
    current = next((e for e in worktrees(root) if e["worktree"] == str(path)), None)
    if current is None or current.get("branch") != entry.get("branch") or "locked" in current:
        return False
    if git(path, "rev-parse", "HEAD") != head:
        return False
    return not git(path, "status", "--porcelain", "--untracked-files=all")


def cleanup(apply=False):
    root = primary_root()
    metadata = json.loads(
        run("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef", cwd=root).stdout
    )
    repo = metadata["nameWithOwner"]
    default = metadata["defaultBranchRef"]["name"]
    git(root, "fetch", "origin", "--prune")
    prs = json.loads(
        run(
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,state,headRefName,headRefOid,mergeCommit,isCrossRepository",
            cwd=root,
        ).stdout
    )
    local = dict(
        line.split(" ", 1)
        for line in git(
            root, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"
        ).splitlines()
    )
    remote = {
        ref.removeprefix("refs/heads/"): sha
        for sha, ref in (
            line.split() for line in git(root, "ls-remote", "--heads", "origin").splitlines()
        )
    }
    entries = {e["branch"].removeprefix("refs/heads/"): e for e in worktrees(root) if "branch" in e}
    busy = busy_paths()
    plan = {"repository": repo, "local": [], "remote": [], "kept": []}
    for branch, head in local.items():
        if branch == default:
            continue
        entry = entries.get(branch)
        if eligible(root, branch, head, prs, default) and (
            entry is None or removable(root, entry, head, busy)
        ):
            plan["local"].append(
                {"branch": branch, "head": head, "worktree": entry["worktree"] if entry else None}
            )
        else:
            plan["kept"].append(branch)
    for branch, head in remote.items():
        if eligible(root, branch, head, prs, default, equivalent_tree=False):
            plan["remote"].append({"branch": branch, "head": head})
    print(json.dumps(plan, indent=2))
    if not apply or not (plan["local"] or plan["remote"]):
        return plan

    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")  # noqa: UP017 (Python 3.9 CLI)
    backup = root / ".validation" / "worktree-cleanup" / stamp
    backup.mkdir(parents=True, mode=0o700)
    (backup / "manifest.json").write_text(json.dumps(plan, indent=2))
    refs = ["refs/heads/" + item["branch"] for item in plan["local"]]
    for item in plan["remote"]:
        ref = "refs/remotes/origin/" + item["branch"]
        if git(root, "rev-parse", ref) != item["head"]:
            raise RuntimeError("Remote tip changed; rerun the audit before deleting")
        refs.append(ref)
    if refs:
        git(root, "bundle", "create", str(backup / "branches.bundle"), *refs)
        git(root, "bundle", "verify", str(backup / "branches.bundle"))
    for index, item in enumerate(plan["local"]):
        branch, head = item["branch"], item["head"]
        if item["worktree"]:
            entry = entries[branch]
            path = Path(item["worktree"])
            if not removable(root, entry, head, busy_paths()):
                raise RuntimeError(f"Worktree changed or became busy: {path}")
            archive_data(path, backup / f"{index}-local-data.tar.gz")
            if not removable(root, entry, head, busy_paths()):
                raise RuntimeError(f"Worktree changed during backup: {path}")
            # No force: Git must still consider the worktree removable.
            git(root, "worktree", "remove", str(path))
        if any(e.get("branch") == "refs/heads/" + branch for e in worktrees(root)):
            raise RuntimeError(f"Branch is now checked out: {branch}")
        run("git", "update-ref", "-d", "refs/heads/" + branch, head, cwd=root)
    for item in plan["remote"]:
        ref = "refs/heads/" + item["branch"]
        # A concurrent push must abort deletion instead of discarding new commits.
        git(root, "push", "origin", f"--force-with-lease={ref}:{item['head']}", ":" + ref)
    git(root, "fetch", "origin", "--prune")
    print(f"Cleanup complete. Recovery files: {backup}")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Delete audited merged branches and worktrees"
    )
    args = parser.parse_args()
    cleanup(apply=args.apply)
