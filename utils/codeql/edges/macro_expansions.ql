/**
 * Enumerate every macro expansion site in the database.
 *
 * One row per `MacroInvocation`. The query does NOT filter on
 * `mi.getParentInvocation()` — that filter (intended to
 * deduplicate transitive expansions) silently drops every
 * argument-position constant a port site passes to a wrapping macro,
 * because in C macro semantics each argument token is itself a
 * `MacroInvocation` whose parent is the wrapping macro:
 *
 *   `SSLfatal(s, SSL_AD_INTERNAL_ERROR, SSL_R_BAD_PACKET);`
 *
 * produces four `MacroInvocation` nodes — `SSLfatal`,
 * `SSL_AD_INTERNAL_ERROR`, `SSL_R_BAD_PACKET`, plus nested expansions
 * of `SSLfatal`'s body — and a parent-restricted filter would keep
 * only `SSLfatal`. Every `SSL_R_*`, `BIO_CTRL_*`, `ERR_R_*` constant
 * passed as an argument would vanish. This query records all four.
 *
 * Consumers that need to dedup against transitive expansions of the
 * same macro at the same site should dedup by `(macro_name,
 * invocation_file, invocation_line)`, NOT by parentage.
 *
 * The `enclosing_name` column is "" for file-scope invocations
 * (e.g. `DEFINE_STACK_OF(SSL_SESSION)` at file scope) and the
 * enclosing function's name for
 * function-body invocations. The two forms produce different
 * reachability semantics — function-body macros reach via a port
 * function calling them, file-scope macros reach via a port file
 * containing the instantiation.
 *
 * # cols:
 *   macro_name          : the invoked macro's C identifier
 *   macro_def_file      : repository-relative path of the macro's
 *                         `#define` site
 *   enclosing_name      : enclosing function's C identifier, or "" if
 *                         the invocation is at file scope
 *   invocation_file     : repository-relative path of the invocation
 *                         site's file
 *   invocation_line     : 1-indexed line number of the invocation
 *
 * Consumer: CrustifySymbolAnalyzer (for `called_by.call` / `ref`
 * on macros, and `depends_on.syms.{port,wrap}` on functions whose
 * bodies expand wrap-side macros).
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

string enclosingNameOf(MacroInvocation mi) {
  if exists(mi.getEnclosingFunction())
  then result = mi.getEnclosingFunction().getName()
  else result = ""
}

from MacroInvocation mi, Macro m
where m = mi.getMacro()
select m.getName() as macro_name,
       pathOf(m.getLocation().getFile()) as macro_def_file,
       enclosingNameOf(mi) as enclosing_name,
       pathOf(mi.getFile()) as invocation_file,
       mi.getLocation().getStartLine() as invocation_line
