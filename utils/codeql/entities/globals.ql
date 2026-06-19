/**
 * Enumerate every global variable in the database.
 *
 * Returns one row per `GlobalVariable`, regardless of whether the
 * defining initializer is in the database. Declaration-only
 * `extern` globals (declared in a header but defined in a TU outside
 * the DB) appear with `def_file = ""` — consumers must distinguish
 * them from globals whose definition is in the DB.
 *
 * Linkage class is derived from the variable's properties:
 *   - "global_static"   — file-local linkage (declared `static`)
 *   - "global_extern"   — externally linkable (the default)
 *
 * # cols:
 *   name        : the variable's C identifier
 *   linkage     : "global_static" | "global_extern"
 *   type        : the variable's declared C type as a single string,
 *                 including const-qualifiers and array dimensions
 *                 (e.g. `const unsigned char tls11downgrade[8]`)
 *   def_file    : repository-relative path of the definition's file,
 *                 or "" if no definition is in the DB
 *   decl_files  : pipe-separated list of repository-relative paths
 *                 of all declaration entries (typically the headers
 *                 carrying the `extern` declaration); may be empty
 *
 * Consumer: CrustifySymbolAnalyzer, for global enumeration; also
 * CrustifyTypeAnalyzer when looking up a global's type for
 * `depends_on.types` population.
 *
 * No `hasDefinition()` filter at the outer level — consumers need
 * declaration-only externs to identify boundary-crossing globals.
 * The def_file "" sentinel surfaces the missing-definition case
 * explicitly. This is the same rule as functions.ql; see
 * PITFALLS.md §2026-05-31.
 *
 * Function-local variables and parameters are NOT enumerated here —
 * they are intra-function noise and never cross the wrap/port
 * boundary. The query filters to `GlobalVariable`, which excludes
 * `LocalVariable` and `Parameter` by class.
 */
import cpp

string linkageClassOf(GlobalVariable g) {
  if g.isStatic()
  then result = "global_static"
  else result = "global_extern"
}

/**
 * Repository-relative path, falling back to absolute for files outside the
 * source root (system / external headers) so out-of-root globals are
 * captured under a `system/` manifest dir instead of dropped.
 */
string pathOf(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

string defFileOf(GlobalVariable g) {
  if exists(g.getDefinition())
  then result = pathOf(g.getDefinition().getFile())
  else result = ""
}

string declFilesOf(GlobalVariable g) {
  result = concat(File h |
    h = g.getADeclarationEntry().getFile()
  | pathOf(h), "|"
    order by pathOf(h)
  )
}

from GlobalVariable g
select g.getName() as name,
       linkageClassOf(g) as linkage,
       g.getType().toString() as type,
       defFileOf(g) as def_file,
       declFilesOf(g) as decl_files
