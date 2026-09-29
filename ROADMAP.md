# Roadmap — dispatcher units

One checkbox = one headless session. The agent ticks the matching box as
part of its PR. Chunk numbering follows PLAN.md build steps; every chunk is
an [impl] session.

- [x] [impl] Chunk 1: scan/recognize/apply subcommands + CSV layer + scan (PLAN.md build steps 1-3)
- [x] [impl] Chunk 2: recognize + apply full behavior — crash-safe rewrite, idempotency, --dry-run (steps 4-5)
- [ ] [impl] Chunk 3: --thumbs DIR local-read mode (step 6)
- [ ] [impl] Chunk 4: committed fake-payload test suite (step 7)
