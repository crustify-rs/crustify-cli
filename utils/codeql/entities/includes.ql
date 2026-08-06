/**
 * Enumerate every `#include` edge in the database.
 *
 * One row per `(source_file, included_file)` pair. The source side is
 * the file containing the `#include` directive; the included side is
 * the resolved file the preprocessor expanded into. Paths are
 * relative-when-possible (in-tree files) and absolute-as-fallback
 * (system headers outside the source root). Consumers route the
 * absolute-path rows under `analysis/system/` per `path_partition.py`.
 *
 * Conditional includes guarded by `#ifdef` produce rows only for
 * branches the extractor actually took during the traced build.
 * Multiple `#include`s of the same header from the same source file
 * collapse to a single row (CodeQL select clauses deduplicate
 * implicitly).
 *
 * # cols:
 *   source_file   : path of the file containing the `#include` directive
 *   included_file : path of the included file
 *
 * Consumer: compose/files_manifest.py — joins with file enumeration
 * to emit the `includes` graph per stem-grouped manifest dir.
 */
import cpp

/**
 * Path emission: prefer the source-root-relative path when available
 * (in-tree files), fall back to the absolute path for files outside
 * the source root (system headers, …).
 */
string filePath(File f) {
  if exists(f.getRelativePath())
  then result = f.getRelativePath()
  else result = f.getAbsolutePath()
}

from Include inc, File src, File dst
where inc.getFile() = src
  and inc.getIncludedFile() = dst
select filePath(src) as source_file,
       filePath(dst) as included_file
