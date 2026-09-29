# Development workflow

For substantial feature changes, use subagents:

1. Scout: inspect relevant code without editing. Report dependencies,
   existing conventions, and risks.
2. Coordinator: define the implementation plan and file ownership.
3. Builder: implement the approved scope.
4. Reviewer: independently review the diff for correctness, privacy,
   and missing tests.
5. Builder: fix findings.
6. Coordinator: run final checks and summarize the results.

Keep dependent steps sequential. Parallelize backend and frontend work
only after agreeing on the API contract. Avoid concurrent edits to
the same files.

For small fixes, use a single agent.

## Required checks

- Preserve private-by-default note access.
- Run isolation tests after authorization or retrieval changes.
- Run relevant backend tests and the frontend build.
- Report skipped or blocked checks honestly.
- Preserve unrelated work.
- Do not push or deploy unless requested.
