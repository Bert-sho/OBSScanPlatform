# AGENTS.md

## Project operating rules

This repository is used with Codex App across multiple machines.
Repository state, task state, and handoff notes must be kept in Git so that work can continue from another PC.

Codex must treat the repository as the source of truth, not the chat history.

## Project workflow

Use Superpowers for non-trivial software engineering tasks.

- Unclear feature or product work: use brainstorming.
- Multi-file implementation: use writing-plans before editing.
- Bug, crash, failing test, or build error: use systematic-debugging.
- Behavior-changing implementation: use test-driven-development.
- Before completion: use requesting-code-review and verification-before-completion.

Do not move from design to implementation without explicit user approval when the task started with brainstorming.

## Karpathy-style coding discipline

These rules apply inside every Superpowers workflow.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

## Documentation handoff requirement

At the end of every development task, update both files:

- `docs/current-task.md`
- `docs/handoff.md`

These files are mandatory even if the code change is small.

### `docs/current-task.md` must include

- Current task title
- Current branch
- Task status: `not-started`, `in-progress`, `blocked`, `completed`, or `wip`
- User goal
- Completed work
- Remaining work
- Key files changed
- Validation commands run
- Validation result
- Known risks
- Next recommended action

### `docs/handoff.md` must include

- Timestamp
- Machine/environment if relevant
- Current branch
- Latest commit before this session
- Latest commit after this session
- Summary of what changed
- Important decisions and rationale
- Failed attempts or rejected approaches
- Current test/build status
- Uncommitted changes, if any
- Exact resume instructions for the next Codex session

The handoff must be written for a future AI coding agent, not only for a human reader. It must be specific, actionable, and self-contained.

## Git workflow

Before committing:

```bash
git status
git diff --stat
git diff
```

Run the relevant validation commands before committing whenever possible.

After validation and handoff updates:

```bash
git add .
git commit -m "<type>: <short task summary>"
git push -u origin HEAD
```

Use conventional commit types when possible:

- `feat:` for new features
- `fix:` for bug fixes
- `refactor:` for refactors
- `test:` for tests
- `docs:` for documentation-only changes
- `chore:` for setup, tooling, or maintenance
- `wip:` only when the task is intentionally incomplete

Do not push directly to `main` or `master` unless the user explicitly asks.
Prefer working on a feature branch.

If currently on `main` or `master`, create a branch before making non-trivial changes:

```bash
git switch -c codex/<short-task-name>
```

## Handling failed validation

If validation fails:

1. Try to fix failures that are directly caused by the current task.
2. Do not hide or ignore failures.
3. Record the failure in `docs/current-task.md`.
4. Record the failure and exact failing command in `docs/handoff.md`.
5. If the task cannot be completed safely, commit with `wip:` only if preserving work is useful.
6. Clearly report that the branch contains failing validation.

Never mark a task as `completed` if validation failed.

## GitHub push failure handling

If `git push` fails because of authentication, network, permissions, protected branch rules, or remote divergence:

1. Do not keep retrying blindly.
2. Record the push failure in `docs/handoff.md`.
3. Show the exact command that failed.
4. Show the exact error summary.
5. Leave the repository in a clean or clearly documented state.
6. Tell the user what command to run manually.

## Security boundaries

Never commit secrets.

Do not commit:

- `.env`
- API keys
- tokens
- private SSH keys
- credentials
- local database dumps
- company-internal secrets
- generated dependency folders such as `node_modules`
- Python virtual environments such as `.venv`
- build artifacts unless explicitly required

Before committing, inspect the diff for secrets or machine-specific paths.

## End-of-task checklist

Before ending any development task, Codex must verify:

- [ ] Relevant code changes are complete.
- [ ] Relevant tests or checks were run, or failure reason is documented.
- [ ] `docs/current-task.md` is updated.
- [ ] `docs/handoff.md` is updated.
- [ ] `git status` was checked.
- [ ] Diff was reviewed for unrelated changes and secrets.
- [ ] Changes were committed.
- [ ] Current branch was pushed to GitHub, or push failure was documented.
- [ ] Final response includes branch name, commit hash, validation result, and push status.
