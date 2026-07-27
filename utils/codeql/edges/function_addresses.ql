/**
 * Enumerate every non-call use of a function identifier.
 *
 * One row per `FunctionAccess` that is NOT the callee of a `Call`.
 * This captures the patterns that produce `called_by.ref` entries
 * for functions:
 *
 *   - Explicit address-of: `&foo`
 *   - Callback storage: `cb->handler = foo;`
 *   - Function-pointer initializer entries: `{ .ops = foo, ... }`
 *   - Bare identifier in a non-call context (rare; usually implicit
 *     decay to function pointer)
 *
 * Calls themselves are excluded — they belong to
 * edges/function_calls.ql. Querying both and unioning would
 * double-count the call site (a `Call(foo, args)` contains a
 * `FunctionAccess` for `foo` as its callee expression).
 *
 * # cols:
 *   enclosing_name      : the enclosing function's C identifier; for a
 *                         file-scope access (a static initializer table) the
 *                         VARIABLE being initialised; "" if neither
 *   access_file         : repository-relative path of the access
 *                         site's file
 *   target_name         : the referenced function's C identifier
 *   target_def_file     : repository-relative path of the target's
 *                         definition site, or "" if no definition is
 *                         in the DB
 *   access_line         : 1-indexed line number of the access site
 *
 * Consumer: CrustifySymbolAnalyzer for `called_by.ref` on
 * `function_*` entries; CrustifyTypeAnalyzer for callback-slot
 * field-population discovery (e.g. ops table entries).
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

/**
 * The entity an access belongs to: the enclosing function when there is one,
 * else the file-scope VARIABLE whose initializer contains the access, else "".
 *
 * The variable fallback is what makes a dispatch table a real dependency node.
 * `static const EXTENSION_DEFINITION ext_defs[] = { { ..., tls_parse_ctos_alpn,
 * ... } }` has no enclosing function, so attributing it to "" dropped every
 * one of its function references on the floor — the table looked like a leaf
 * with no forward edges, and the port had to rediscover them by hand.
 */
string enclosingNameOf(Expr e) {
  exists(e.getEnclosingFunction()) and result = e.getEnclosingFunction().getName()
  or
  not exists(e.getEnclosingFunction()) and
  exists(Variable v | v.getInitializer().getExpr().getAChild*() = e | result = v.getName())
  or
  not exists(e.getEnclosingFunction()) and
  not exists(Variable v | v.getInitializer().getExpr().getAChild*() = e) and
  result = ""
}

from FunctionAccess fa, Function target
where target = fa.getTarget()
// In cpp-all, direct calls (`foo(x)`) do NOT produce a separate
// FunctionAccess entity — the FunctionCall IS the access. A
// FunctionAccess entity only materialises when the function
// identifier appears in a non-call context (`&foo`, `cb = foo`,
// initializer entries). So no explicit exclude-the-callee filter is
// needed here; the call/non-call split is already disjoint at the
// entity level.
select enclosingNameOf(fa) as enclosing_name,
       pathOf(fa.getFile()) as access_file,
       target.getName() as target_name,
       defFileOf(target) as target_def_file,
       fa.getLocation().getStartLine() as access_line
