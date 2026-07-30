/**
 * Enumerate every Macro entity in the database.
 *
 * One row per Macro. The macro's head (parameter list as written
 * after the name in the `#define`, or "" for object-like macros) and
 * body text are emitted verbatim so a consumer can read what a macro
 * expands to without re-querying.
 *
 * Conditionally redefined macros produce one row per `#define`
 * site. Consumers that need a single canonical row per name dedup at
 * composition time.
 *
 * # cols:
 *   name      : the macro's C identifier
 *   head      : the whole head of the `#define`, INCLUDING the name —
 *               `AF_INET` for an object-like macro, `MAX(a, b)` for a
 *               function-like one. Never empty for a named macro, so
 *               "is this callable?" is the presence of a `(`, not
 *               emptiness (a zero-arg `F()` reads correctly this way)
 *   body      : the replacement-list text, verbatim from the source
 *   def_file  : repository-relative path of the `#define` site's file
 *
 * Consumer: CrustifySymbolAnalyzer, for macro discovery + body-based
 * kind classification.
 *
 * No invocation-related filtering here — this query enumerates
 * macros that EXIST. Whether a macro is REACHED from port-scope code
 * is the job of edges/macro_expansions.ql, joined against this
 * output at composition time.
 */
import cpp

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system / external headers) so out-of-root macros are
 * captured under a `system/` manifest dir instead of dropped.
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

from Macro m
select m.getName() as name,
       m.getHead() as head,
       m.getBody() as body,
       pathOf(m.getLocation().getFile()) as def_file
