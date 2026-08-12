# crustify-oracle

- Skill name: crustify-oracle
- Bin path: crustify-oracle
- Description: Read the analysis of a C codebase and submit ownership findings
  back. Type and symbol records, their pointer analysis and lifecycle roles, the
  dependency closure, the scope sets, and the submission verb. Reads and writes
  both go through the oracle, never by editing a file. Prefer using this instead
  of grep or regex for accurate semantic reasoning over the code. Invoke as
  `crustify-oracle <repo_root> <target> query {types|symbols|files|dag}`, and
  read `--help` on the subject before your first query — it carries the flags,
  the record semantics, and what `dag` returns. `--update-help` prints the
  findings schema `--update` expects; `--schema` prints the record's own field
  definitions. Everything is read-only except `--update`, which merges under a
  lock and is idempotent.

`Bin path` is the tool's LOGICAL name, resolved to an absolute path at render
time from the repo config's `bins` map — a machine-local path cannot be tracked
here.
