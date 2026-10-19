# Repository instructions

## Git workflow

- Perform every task that may modify tracked files in a dedicated, isolated Git worktree. Never edit the primary checkout directly.
- If an isolated worktree cannot be created or used, stop and explain the blocker instead of falling back to the primary checkout.
- Worktrees are sibling working directories attached to the same repository; never create a worktree inside another worktree.
- Create every new branch as `feature/<name>`, where `<name>` is a short, descriptive, lowercase kebab-case slug.
- For an independent change, start the branch from the repository's current default branch unless the user explicitly selects another base.
- Never commit directly to `main`, `master`, `develop`, or another protected branch.
- Preserve unrelated user changes and do not rewrite remote history or force-push without explicit authorization.

## Commits and pull requests

- Each pull request may change at most 12 files, counting additions, modifications, deletions, configuration, tests, documentation, and lockfiles against its immediate base branch.
- Split work into sequential, coherent groups. Complete and review each pull request before starting the next dependent group.
- Group each commit by responsibility. Prefer multiple meaningful commits in a pull request; never combine unrelated groups merely to create a single commit.
- Keep automatic deletion of remote source branches enabled after pull requests are merged. Do not recreate a merged branch merely to preserve its name; use the pull request to inspect its original commits.
- After each squash merge, automatically remove eligible merged local branches and their worktrees, including older merged branches. Keep the primary checkout and any open, closed-without-merge, dirty, locked, active, or divergent worktree.
- Use `python3 scripts/merge_pr.py <PR-number>` from the primary checkout to merge, fast-forward main, and run cleanup automatically. For a merge performed on GitHub, finalize locally with the same command; GitHub cannot remove directories on this computer.
- Cleanup must verify merged PR evidence against current main, including tree equality for squash/rebased branches. Preserve branch history in a Git bundle and non-rebuildable ignored data in restricted local backups under `.validation/worktree-cleanup/` before removal. Never print credentials or commit these backups.
- `python3 scripts/cleanup_merged.py` audits without deleting; `--apply` performs the authorized cleanup. Preserve unmerged work and report any skipped branches instead of forcing removal.
- Review each finished pull request and run checks appropriate to its stage before proceeding. State any validation that requires a later integration layer.
- Immediately after creating any pull request, post a comment mentioning `@claude` and explicitly ask Claude to review the pull request.
- Wait for Claude to finish its review and read all review feedback, including inline comments. A pending review, an acknowledgment, or no response does not count as a completed review; if Claude is unavailable, report the blocker and do not proceed.
- Evaluate each comment against the code, requirements, and product goals. Implement feedback that is valid and improves the product; explain on the pull request why any suggestion is not applicable or would not improve it instead of applying it blindly.
- Address all actionable feedback, run the relevant checks for any changes, and reply to the review comments with the outcome. Resolve review threads only after their concerns have been addressed. Continue to the next dependent group or merge only after this review cycle is complete and the existing check and approval requirements are satisfied.

- Use Conventional Commits for every commit: `<type>(<optional-scope>): <description>`.
- Prefer these types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`, and `revert`.
- Keep the subject concise and imperative. Mark breaking changes with `!` and a `BREAKING CHANGE:` footer when applicable.
- Use a Conventional Commit title for every pull request so it can become the final squash commit message.
- Integrate pull requests exclusively with squash merge. Do not use merge commits or rebase merge.
- Every squash commit on the default branch must end with its pull request reference, ` (#<number>)`, so the branch that produced it stays traceable from `git log`.
- Never pass `--subject` or `--body` to `gh pr merge --squash`. GitHub appends ` (#<number>)` only when it builds the subject from the pull request title; an explicit subject silently drops it.
- If the pull request title is not the message you want on the default branch, correct it with `gh pr edit --title` before merging, then merge without overriding the subject.
- Merge only after required checks and approvals pass and all conflicts and actionable review comments are resolved.
- After merging, verify that the new commit on the default branch carries its ` (#<number>)` suffix. Repairing a missing one means rewriting published history, which needs explicit authorization.

## Stacked pull requests

- Use a stacked pull request only when a change genuinely depends on another unmerged change. Keep independent changes based on the default branch.
- Keep every stack in one repository as a single linear chain. The bottom branch targets the stack trunk, and each higher branch starts from and targets the branch immediately below it.
- Give every stack layer its own focused `feature/<name>` branch, isolated sibling worktree, Conventional Commit history, and reviewable pull request.
- Never describe this as a “worktree on a worktree.” Create the higher worktree from the lower branch ref while keeping both worktree directories separate.
- When stack branches are checked out in separate worktrees, use `gh stack link` or the GitHub website to create or link the stack. Do not run `gh stack checkout`, `gh stack rebase`, or `gh stack sync` across branches held by other worktrees.
- Apply corrections to the lowest layer that owns the change, then update dependent layers in order and verify the resulting ancestry and diffs.
- Merge stacked pull requests from bottom to top using squash merge. After each merge, verify that GitHub correctly rebases or retargets the remaining stack before continuing.


## Documentation

- Write READMEs, guides, contribution templates, and diagram labels in English.
- Prefer concise Mermaid flows, sequence diagrams, and comparison tables over long prose.
- Keep diagrams aligned with code and label planned capabilities explicitly.
- Preserve runnable Spanish customer utterances where required by the Colombia demo; explain them in English.
