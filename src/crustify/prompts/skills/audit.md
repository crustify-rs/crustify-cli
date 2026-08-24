<!-- SKILL -->

For a Crustify translation, invoke only `crustify-audit <repo_root> unsafe`;
never invoke `crustify-audit ub`. Seed `unsafe` with the exact scheduled C type
and symbol names and request JSON. Type entries expose raw-pointer and raw-deref
sites plus manual `Deref`/`DerefMut` and materialized shared/mutable slice sites;
symbol entries expose declaration/body raw-pointer sites and body dereferences.
Treat every site as an investigation lead, not a verdict. The generated
`crustify/audit/unsafe.json` is gitignored.
