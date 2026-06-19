/**
 * GENERATED FILE — do not hand-edit.
 *
 * Regenerate with:
 *   python3 utils/codeql/generate_port_scope.py <target>
 *
 * Source of truth: <target>/.crustify/analysis/scope/port/files.json
 *
 * Exports:
 *   - portFile(File f)   — f's relative path is in the port-scope set.
 *   - portPath(string p) — p is in the port-scope set.
 *
 * Consumed by every query in utils/codeql/ that needs scope
 * partitioning. The path set is inlined here so queries are
 * self-contained at evaluation time; no --external flag required.
 */
import cpp

predicate portPath(string p) {
  p = "include/internal/statem.h" or
  p = "ssl/statem/extensions.c" or
  p = "ssl/statem/extensions_clnt.c" or
  p = "ssl/statem/extensions_cust.c" or
  p = "ssl/statem/extensions_srvr.c" or
  p = "ssl/statem/statem.c" or
  p = "ssl/statem/statem_clnt.c" or
  p = "ssl/statem/statem_dtls.c" or
  p = "ssl/statem/statem_lib.c" or
  p = "ssl/statem/statem_local.h" or
  p = "ssl/statem/statem_srvr.c"
}

predicate portFile(File f) {
  portPath(f.getRelativePath())
}
