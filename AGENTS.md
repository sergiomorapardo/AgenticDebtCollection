# Repository instructions

## Git workflow

- Perform every task that may modify tracked files in a dedicated, isolated Git worktree. Never edit the primary checkout directly.
- If an isolated worktree cannot be created or used, stop and explain the blocker instead of falling back to the primary checkout.
- Worktrees are sibling working directories attached to the same repository; never create a worktree inside another worktree.
- Create every new branch as `feature/<name>`, where `<name>` is a short, descriptive, lowercase kebab-case slug.
- For an independent change, start the branch from the repository's current default branch unless the user explicitly selects another base.
- Never commit directly to `main`, `master`, `develop`, or another protected branch.
- Before starting changes and immediately before each squash merge, run `git pull --ff-only origin main` from the clean primary checkout. If it cannot fast-forward, inspect the divergence and preserve local work; never reset or force the update.
- Update the isolated feature branch against the refreshed default branch and resolve conflicts before merging. For a stack, update its bottom layer against the default branch, then propagate changes through each immediate base in order. Do not bypass existing authorization requirements for history rewrites or force-pushes.
- After updating branches or resolving conflicts, rerun relevant checks and re-request review when the changes require it. Verify each PR still changes at most 12 files against its immediate base.
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
- The review unit is each individual PR against its immediate base, never a group of pushes or an entire stack. Each PR needs its own completed Claude review; a review of another PR cannot satisfy that requirement.
- Optimize total Claude Opus executions per PR, including automatic push-triggered runs and manual mentions. Prepare and self-review that PR's complete diff, run relevant local checks, and batch all findings from a review round of that same PR before publishing its next substantive revision. Reducing mentions alone is not sufficient.
- Before every push, account for automatic review triggers and dependent branches. Complete the current PR's review cycle before advancing to the next dependent layer; do not trigger reviews across other PRs as if a group of pushes were a review unit.
- Before integrating independent PRs, inspect their bases, completed reviews, and automatic triggers. Choose an order that avoids invalidating completed reviews by merging documentation or another independent change first. Do not mechanically rebase or push an already reviewed PR without assessing whether the update is required and the cost of another review; still perform required base updates. Inform the user before causing an additional review and explain why it is needed.
- An explicit user exception to the reviewer requirement applies only to the identified PR. Record the exception, completed checks, and remaining limitations on that PR; it does not waive review for future PRs.
- Before requesting Claude review, inspect automatic and manual reviews on the same PR for the same head and immediate-base commits. Reuse an equivalent in-progress or completed review; do not trigger a duplicate. This applies separately to new PRs, each stacked layer, and subsequent revisions, and supersedes an unconditional mention after PR creation.
- If no equivalent review exists, request one with a single concise `@claude` comment containing the scope, head/base SHAs, check results, and resolved or outstanding findings. Do not mention Claude in routine thread replies, commits, cosmetic updates, or status queries.
- Wait for the selected automatic or manual workflow to finish successfully and for Claude to publish its completed review; read all feedback, including inline comments. A successful workflow without a completed review, an acknowledgment, or no response does not satisfy this requirement. Saving quota never waives the final review.
- If the selected workflow fails or is cancelled, or no completed review is available within 15 minutes, report the blocker and investigate or retry as appropriate. Count from the review-request comment creation time for manual reviews, or from the workflow run creation time for automatic reviews (including queue time). Reusing a run does not reset this deadline. Elapsed time does not waive the review requirement; do not proceed until the review is complete.
- Evaluate each comment against the code, requirements, and product goals. Implement feedback that is valid, improves the product, and is in scope for this PR. Explain on the pull request why a suggestion is not applicable; record useful out-of-scope suggestions as follow-up work without exceeding the 12-file limit. Fix blocking issues before proceeding.
- Address all actionable feedback on the same PR and run the relevant checks for any changes. The PR author or agent must reply with the outcome and resolve review threads only after their concerns have been addressed. After batching that PR's substantive fixes to behavior, contracts, or workflow requirements, obtain a completed review of its updated revision, reusing an equivalent automatic review on that PR before adding another mention. Repeat its cycle for new actionable findings. Request again only when that PR has a substantive revision needing review or an investigated failure left no completed review; spelling or formatting-only fixes do not require another review.
- Continue to the next dependent group or merge only after this review cycle is complete and the existing check and approval requirements are satisfied.

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

- Never publish private client or training-project names, repository links, internal commit identifiers, or client-specific material in public repositories, commit messages, PR descriptions, or review comments. Use project-local documentation and inspect the full diff and public metadata before publishing.
- Write READMEs, guides, contribution templates, and diagram labels in English.
- Prefer concise Mermaid flows, sequence diagrams, and comparison tables over long prose.
- Keep diagrams aligned with code and label planned capabilities explicitly.
- Preserve runnable Spanish customer utterances where required by the Colombia demo; explain them in English.
