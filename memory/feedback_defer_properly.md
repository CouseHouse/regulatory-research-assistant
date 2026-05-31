---
name: feedback-defer-properly
description: TODO comments in shipped code are not a substitute for future-work.md entries — deferred items must be tracked there
metadata:
  type: feedback
---

Don't leave `# TODO: wire in Day N` or `# wired in Day N` comments in code as the only tracking mechanism for deferred work.

**Why:** Code comments are invisible to planning. The Day 3 commit left `trace_id=None  # Langfuse wired in Day 4` and made no entry in `future-work.md`. The result: a blind spot during retrieval pipeline tuning, caught only in a follow-up session.

**How to apply:** When making a unilateral decision to defer a feature:
1. Either ship it now, OR
2. Add an entry to `docs/future-work.md` with the standard shape (status, why deferred, reopen trigger, implementation path, cost).

Code comments can add context about *why* something is incomplete, but the *tracking* must live in `future-work.md`. A comment that says "wired in Day N" is a promise, not a ticket.
