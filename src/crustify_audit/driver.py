"""driver.py — the deterministic pass.

Runs the canonical rustc driver (``src/driver/``) over the subject workspace
and returns its tree-wide metrics block.

Resolution-aware because the questions are: which C type a pointer points at,
whether a reference covers memory C writes, what survived `cfg` and macro
expansion. None of that is answerable from source text.

WHAT IT COSTS. A rustc driver has to compile the crate, and an FFI wrapper
often needs system libraries that are not installed. When the build fails there
are NO counts rather than substitute ones: ``compose`` reports ``counts: null``
and says why. A number absent is recoverable; a number that looks like
crustify-cli's but was produced differently is not.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_DRIVER_CRATE = Path(__file__).resolve().parents[1] / "driver"

# Count fields summed across the per-crate emissions, and site arrays
# concatenated. The set the driver emits; kept explicit so a field added there
# is a visible edit here rather than a silent passthrough.
_COUNTS = (
    "unsafe_blocks", "unsafe_block_stmts", "unsafe_block_lines",
    "unsafe_block_code_lines", "unsafe_blocks_wrapper_impl",
    "unsafe_blocks_ffi_export", "unsafe_fns", "unsafe_fns_seam",
    "unsafe_fns_pub", "unsafe_impls", "unsafe_traits", "ffi_calls",
    "wrapper_newtypes", "wrapper_newtypes_declared",
    "wrapper_declared_nonconformant", "wrapper_newtypes_undeclared",
    "raw_ptr_args", "raw_ptr_rets", "raw_ptr_seam", "raw_ptr_wrapped",
    "raw_ptr_in_wrapper", "ref_to_type_wrapper", "field_ref_wrapped",
    "field_proj_wrapped", "field_proj_outside_impl", "void_ptr_sanctioned",
    "void_ptr_smell", "raw_ptr_derefs", "raw_ptr_derefs_outside_impl",
    "total_stmts", "code_lines",
)
_SITES = ("raw_ptr_sites", "void_ptr_sites", "field_proj_sites",
          "field_ref_sites", "raw_deref_sites")


class DriverUnavailable(Exception):
    """The driver could not measure this tree. Carries the reason to report."""


def _collect_emissions(stdout: str) -> tuple[dict, list[dict], int]:
    """Merge driver JSON lines; return counts, seed entries, crate count."""
    out = {k: 0 for k in _COUNTS}
    sites = {k: [] for k in _SITES}
    entries: list[dict] = []
    seen = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and '"crate"' in line):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        crate = d.get("crate", "")
        if crate.endswith("_sys"):
            continue
        if "seeds" in d:
            for seed in d["seeds"]:
                seed["crate"] = crate
                entries.append(seed)
            continue
        if "unsafe_blocks" not in d:
            continue
        seen += 1
        for k in _COUNTS:
            out[k] += int(d.get(k, 0))
        for k in _SITES:
            sites[k].extend(d.get(k, []))
    out.update(sites)
    return out, entries, seen


def _driver_bin() -> Path:
    """Build the driver if it is not current, and return its path.

    Needs nightly with ``rustc-dev`` and ``llvm-tools`` — pinned by the crate's
    own ``rust-toolchain.toml``.

    ALWAYS asks cargo, rather than reusing whatever binary is on disk. A
    `rustc_private` driver links against the exact compiler it was built with,
    and nightly moves daily — so a binary built last week against a different
    nightly loads, runs, and dies on an undefined symbol. Cargo's fingerprint
    already tracks the compiler version, so this is a no-op when nothing
    changed and a rebuild exactly when one is needed.

    Reusing a stale binary is worse than failing: the symbol error surfaces as
    a non-zero `cargo build` in :func:`measure`, which reports it as "the
    workspace does not compile" — a true statement about the wrong program.
    """
    bin_path = _DRIVER_CRATE / "target" / "debug" / "crustify-audit-driver"
    if shutil.which("cargo") is None:
        if bin_path.is_file():
            return bin_path
        raise DriverUnavailable(
            "the driver is not built and `cargo` is not on PATH")
    if not bin_path.is_file():
        print("[crustify-audit] building the HIR driver (first run only)…")
    r = subprocess.run(["cargo", "+nightly", "build"], cwd=_DRIVER_CRATE,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise DriverUnavailable(f"driver build failed:\n{r.stderr[-1500:]}")
    return bin_path


def _bust_cache(ws: Path) -> None:
    """Touch every workspace target so cargo recompiles.

    The driver only emits while rustc actually compiles a crate, and nothing
    about running it is in cargo's fingerprint — so a second audit over an
    unchanged tree would be served from cache and measure nothing.

    The roots come from cargo rather than a glob: a glob has to assume both a
    depth and a `src/` directory, and a workspace owes neither. git2-rs breaks
    both at once — `git2` is the root crate and `libgit2-sys` sets
    `path = "lib.rs"`.
    """
    meta = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        cwd=ws, capture_output=True, text=True)
    if meta.returncode != 0:
        raise DriverUnavailable(
            f"`cargo metadata` failed:\n{meta.stderr[-1200:]}")
    for pkg in json.loads(meta.stdout)["packages"]:
        for tgt in pkg["targets"]:
            root = Path(tgt["src_path"])
            if root.is_file():
                root.touch()


def measure(ws: Path, names: list[str] | None = None) -> tuple[dict, list[dict]]:
    """Return ``(tree-wide counts, named seed entries)`` for ``ws``.

    Type and symbol names resolve independently inside each compiled workspace
    crate. Entries therefore retain their crate name.
    """
    driver = _driver_bin()
    sysroot = subprocess.run(["rustc", "+nightly", "--print", "sysroot"],
                             capture_output=True, text=True).stdout.strip()
    if not sysroot:
        raise DriverUnavailable("no nightly toolchain (`rustc +nightly`)")
    _bust_cache(ws)

    env = dict(os.environ)
    env["RUSTC"] = str(driver)
    env["SYSROOT"] = sysroot
    ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{sysroot}/lib" + (f":{ld}" if ld else "")
    if names:
        env["UM_MODE"] = "seed"
        env["UM_SEED_NAME"] = " ".join(names)

    r = subprocess.run(["cargo", "+nightly", "build"], cwd=ws, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise DriverUnavailable(
            "the workspace does not compile under the HIR auditor — an FFI "
            "wrapper usually needs its system libraries installed.\n"
            f"{r.stderr[-1200:]}")

    # `-sys` crates are generated bindings, never the audit subject.
    out, entries, seen = _collect_emissions(r.stdout)
    if not seen:
        raise DriverUnavailable(
            "no crate emitted metrics — the build was served from cache, so "
            "nothing was measured")
    return out, entries
