---
name: dsh-work
description: Review, integrate, and explicitly clean up containerize-dsh tasks from host Codex when invoked as $dsh-work.
---

Maintain this skill in `skills/dsh-work/` in the `containerize-dsh` repository.
The user-level installation is a generated copy: edit the canonical source and
rerun `python scripts/install_workflow.py` from that repository to install/update.
The installed `.containerize-dsh-install` file identifies the source checkout.
Keep project-specific instructions and credentials out of this skill.

This skill runs on the **HOST**. DeepSeek runs inside Docker and must never be
asked to manipulate the host checkout. Use `dsh-task` for deterministic task
operations; do not recreate Docker setup, profile/plugin management, or Git
bundle transfers. If the command is not on this process's PATH, read the installed
[launcher location](references/launcher.txt) and invoke that absolute command.

Resolve the supplied task ID with `dsh-task show <id>`. Read the target
repository's applicable instructions. Manifest paths and profile settings are
user-local, never material to copy into this shareable skill. Do not run parallel
operations on one task or use the lower-level launcher while managing it here.

## Review

Run `dsh-task export <id>`: it verifies ownership, stops the task, snapshots tracked
work, retains a recovery bundle, and imports a fresh dedicated review branch.
It intentionally leaves DeepSeek stopped. If new files are reported, inspect the
list and establish which are intended; use `--include-untracked` only when all
listed new files are intended. Otherwise leave the task paused and resolve file
selection with the user. Ignored files remain outside bundles and block cleanup.

Read the latest review in `show`. Review `git diff <base> <review-revision>` in the
recorded source repository and `git log <base>..<review-revision>`. Separately
inspect `<transfer_host>..<base>` to understand any pre-existing dirty snapshot.
Never compare against an assumed main branch, import that snapshot as agent work,
or treat the agent's completion summary as verification evidence.

For inspection/testing, create an optional detached worktree under the task's
state directory using a fresh path. Record its path in the review report; remove
it normally only after checking tracked, untracked, and ignored files. Inspect
relevant tests and actual execution evidence. Report actionable findings and
what remains unverified. Do not fix implementation code, update the target, or
delete the sandbox in review mode.

Write the report to a local file, including the exact revision, findings, tests
and outcomes, and limitations. Record it with
`dsh-task reviewed <id> --revision <oid> --report <file>`. This records evidence,
not automatic approval of findings. Never mark a revision as safe merely because
the helper accepts a report. Repeated exports preserve earlier branches/bundles.
Use `dsh-task resume <id>` only if the user wants DeepSeek to continue.

## Integrate

Export and review the exact current work. An earlier report can inform review
only after verifying identical base, work tree, and committed history; record a
report for the new export revision. Address unresolved findings with the user
before applying changes; review alone never authorizes fixes.

Run `dsh-task prepare <id>`. It applies only the base-to-agent diff in a fresh
integration worktree at the current intended target, commits those changes on
a prepared branch, and records the exact revision. A baseline-dependent patch
or conflict leaves the worktree and recovery branch intact. Do not silently
resolve this by importing the synthetic base or committing existing host edits.
Explain which baseline dependency must be resolved; if necessary have the user
finish their baseline or authorize a specific resolution, then export/review and
prepare again. Integration remains incomplete while blocked.

Inspect the prepared revision against its target parent and the reviewed agent
delta, especially after target advancement or a dirty baseline. Follow repository
instructions and use its MCP where appropriate. Run relevant project checks in
that integration worktree; keep a report with exact prepared OID, commands,
outcomes and remaining limitations. Do not claim failed/unrun checks passed.
If checks pass and the prepared revision is acceptable, run
`dsh-task finalize <id> --checks <file>`. It fast-forwards only the recorded target
in the original checkout when clean and unchanged. It refuses dirty work, a moved
target, or another checked-out branch and retains the prepared branch. Never
automatically stash, reset, clean, or overwrite host work to bypass a refusal.
Report a blocker as incomplete integration, not success.

## Explicit cleanup

Only clean up when requested, including “integrate and clean up.” That request
authorizes routine task-owned cleanup after successful checks; do not ask again.
Run `dsh-task cleanup <id> --require-integrated` after requested integration;
for standalone requested cleanup use `dsh-task cleanup <id>`.

The helper requires a reviewed, durably imported export, unchanged workspace,
no ignored files, and preserved branch history. It removes only the recorded
container, workspace volume, and safe recorded integration worktrees. It retains
recovery bundles/refs/reports, task records, home/session history, credentials,
plugins, shared images/caches, and networks. Failed integration blocks cleanup.
New/ignored artifacts must be inspected and valuable material archived outside
the checkout; never assume build outputs are disposable. After removing archived
ignored files, export/review again so the cleanup audit matches. Inspect and
normally remove any optional review worktree you created, without `--force`.
Never prune Docker broadly or delete volumes by wildcard. If cleanup refuses,
retain recovery resources and explain the specific blocker.
