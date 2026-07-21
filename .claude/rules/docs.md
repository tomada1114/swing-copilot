---
paths:
  - "docs/**/*.md"
  - "README.md"
  - "CONTRIBUTING.md"
  - "CHANGELOG.md"
---

- Document non-obvious behavior, architecture decisions, and trade-offs
- Do NOT document what is obvious from the code or already expressed by the type system
- Code examples in docs must be valid Python that works with the current API
- Use admonitions (note, warning, tip) for important callouts in MkDocs pages
- Japanese and English prose are both allowed. Keep one language internally
  consistent within a document unless a UI label or quotation requires another
- When behavior changes, update the canonical requirement/design and name the
  regression scenario that enforces it; do not leave the correction only in a
  goal prompt or final report
- Treat `docs/goal-prompts/**` as run-specific support/history. Volatile facts
  such as test counts, coverage percentages, status, and worktree cleanliness
  must come from fresh commands rather than copied prose
- If a design example and the implemented schema/API disagree, record and
  resolve the divergence instead of silently declaring either one authoritative
