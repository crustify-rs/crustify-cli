---
name: crustify-oracle
bin: crustify-oracle
description: >-
  Read the analysis of a C codebase and submit ownership findings back. Type and
  symbol records, their pointer analysis and lifecycle roles, the dependency
  closure, the scope sets, and the submission verb. Reads and writes both go
  through the oracle, never by editing a file. Prefer using this instead of grep
  or regex for accurate semantic reasoning over the code.
---

# `crustify-oracle`

```
crustify-oracle <repo_root> <target> {extract-ql | query {types|symbols|files|dag}}
```

**`--help` is the authority — read it before your first query.** Each subject
documents its own flags, the record semantics you have to get right, and what
`dag` returns and how its groups route the work:

```
crustify-oracle <repo_root> <target> query {types|symbols|files|dag} --help
```

Two more, when you come to submit: `--update-help` prints the findings JSON
schema `--update` expects for that subject, and `--schema` prints the record's
own field definitions.

Everything is read-only except `--update`, which validates a findings doc
against the composed record, merges it under a lock, and leaves untouched slots
as they are. Re-submitting is idempotent. Never edit the store by hand.
