"""Squash a reviewed PR, update the clean primary checkout, then clean merged worktrees."""

import argparse
import json
import os

from cleanup_merged import cleanup, git, primary_root, run


def merge_pr(number):
    root = primary_root()
    metadata = json.loads(run("gh", "repo", "view", "--json", "defaultBranchRef", cwd=root).stdout)
    default = metadata["defaultBranchRef"]["name"]
    if git(root, "branch", "--show-current") != default or git(root, "status", "--porcelain"):
        raise RuntimeError("The primary checkout must be clean and on the default branch")
    git(root, "fetch", "origin", default)
    git(root, "merge", "--ff-only", f"origin/{default}")
    pr = json.loads(
        run("gh", "pr", "view", str(number), "--json", "state,headRefOid", cwd=root).stdout
    )
    if pr["state"] == "OPEN":
        run("gh", "pr", "checks", str(number), "--required", cwd=root)
        # GitHub enforces required approvals and checks; never bypass or override the title.
        run(
            "gh",
            "pr",
            "merge",
            str(number),
            "--squash",
            "--match-head-commit",
            pr["headRefOid"],
            cwd=root,
        )
    elif pr["state"] != "MERGED":
        raise RuntimeError("Only an open or already merged PR can be finalized")
    merged = json.loads(
        run("gh", "pr", "view", str(number), "--json", "state,mergeCommit", cwd=root).stdout
    )
    if merged["state"] != "MERGED":
        raise RuntimeError("Merge has not completed; cleanup was not run")
    git(root, "fetch", "origin", default)
    git(root, "merge", "--ff-only", f"origin/{default}")
    commit = merged["mergeCommit"]["oid"]
    if not git(root, "show", "-s", "--format=%s", commit).endswith(f" (#{number})"):
        raise RuntimeError("Squash commit is missing its PR reference")
    # Cleanup runs from main, never inside the worktree being removed.
    os.chdir(root)
    cleanup(apply=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("number", type=int)
    args = parser.parse_args()
    merge_pr(args.number)
