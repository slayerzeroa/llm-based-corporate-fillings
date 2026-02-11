# Repository automation rules

- After code changes, run tests and lint.
- If tests pass and there are changes:
  1. git add -A
  2. git commit with Conventional Commit style
  3. git push to current branch
- Never push directly to main/master.
- If tests fail, do not commit/push. Report failure.
