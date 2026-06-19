/**
 * Enumerate every function-call edge in the database.
 *
 * One row per `Call`. The query starts from `Call`, NOT from
 * `Function` — this is load-bearing and is the difference between
 * "find all functions that are both called and defined" (the
 * over-restrictive shape that produced the §2026-05-31 PITFALL) and
 * "find every call edge, whatever the target's body status"
 * (correct). Callees whose body is not in the DB (declaration-only
 * externs, assembly-displaced C fallbacks, libc) appear with
 * `callee_def_file = ""`.
 *
 * Scope partitioning is NOT applied here — the analyzer joins this
 * edge stream against `port_scope.qll` (T3) at composition time. Two
 * useful joins:
 *
 *   - port_caller_file → wrap_reachability: filter to rows where
 *     caller_file is port-scope; the resulting callee set is the
 *     "port-reaches-wrap" boundary.
 *   - port_callee_file → into_port_callers: filter to rows where
 *     callee_def_file is port-scope; the resulting caller set is
 *     "wrap (or port) reaches port", i.e. the inverse for the port
 *     manifest's `called_by` field.
 *
 * # cols:
 *   caller_name      : enclosing function's C identifier
 *   caller_file      : repository-relative path of the call site's
 *                      file
 *   callee_name      : called function's C identifier
 *   callee_def_file  : repository-relative path of the callee's
 *                      definition site, or "" if the callee has no
 *                      definition in the DB
 *   call_line        : 1-indexed line number of the call site
 *
 * Consumer: CrustifySymbolAnalyzer (for `called_by.call` and
 * `depends_on.syms` population); CrustifyTypeAnalyzer (for op
 * candidate discovery via inverted callee → caller lookup).
 *
 * Indirect calls through function pointers are recorded with the
 * static target the call resolves to when CodeQL can determine it;
 * if the target is unresolved, the row is dropped (no edge can be
 * attributed). Captures of function addresses without a call are
 * the job of edges/function_addresses.ql, not this query.
 */
import cpp

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system/external headers) — keeps system entities' identity
 * consistent with the T1 entity CSVs.
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

string defFileOf(Function fn) {
  if exists(fn.getDefinition())
  then result = pathOf(fn.getDefinition().getFile())
  else result = ""
}

from Call c, Function callee, Function caller
where
  callee = c.getTarget() and
  caller = c.getEnclosingFunction()
select caller.getName() as caller_name,
       pathOf(c.getFile()) as caller_file,
       callee.getName() as callee_name,
       defFileOf(callee) as callee_def_file,
       c.getLocation().getStartLine() as call_line
