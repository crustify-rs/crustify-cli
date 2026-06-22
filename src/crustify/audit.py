"""Orchestration for the ``audit`` command.

A **deterministic** pass (no LLM): an **entity-seeded, global** scan of the
ported Rust tree. Seeds are types/symbols (``--name``) — or every entity homed
under a ``--file`` / ``--dir`` / ``--crate``, or ``--all``. For each seed it
reports its own implementation's unsafe/raw-pointer surface and its **naked
``ffi::`` footprint** (the wrapper being bypassed elsewhere). Printed to the
console as JSON — nothing written to disk. See
``utils/codeql/compose/audit_manifest.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


def audit(
    target: Path,
    *,
    all: bool = False,
    names: list[str] | None = None,
    crate: str | None = None,
    mod: str | None = None,
    file: str | None = None,
    dir: str | None = None,
) -> None:
    """Audit the seed set and print the report (deterministic; console-only).

    Exactly one selector: ``all`` / ``names`` / ``crate`` / ``mod`` /
    ``file`` / ``dir``. ``--file`` / ``--dir`` / ``--crate`` / ``--mod`` resolve
    to the entities *homed* there; the naked-FFI search is always global.

    Driven by the resolution-aware rustc pass (``utils/unsafe_metrics``), which
    reports precise file:line sites; requires a buildable workspace."""
    selectors = [all, bool(names), crate is not None, mod is not None,
                 file is not None, dir is not None]
    if sum(bool(x) for x in selectors) != 1:
        raise SystemExit(
            "audit: pass exactly one of --all / --name / --crate / --mod / "
            "--file / --dir.")

    sel = ("--all" if all else f"--name {' '.join(names)}" if names else
           f"--crate {crate}" if crate else f"--mod {mod}" if mod else
           f"--dir {dir}" if dir else f"--file {file}")
    _audit_hir(target, sel, all=all, names=names, crate=crate,
               mod=(mod or dir), file=file)


# Global-section count fields merged (summed) across the per-crate emissions.
_GLOBAL_COUNTS = (
    "unsafe_blocks", "unsafe_block_stmts", "unsafe_block_lines",
    "unsafe_block_code_lines", "unsafe_blocks_wrapper_impl", "wrapper_impl_macro",
    "wrapper_impl_handwritten", "unsafe_blocks_ffi_export", "rp_wrap_nonseam_args",
    "rp_wrap_nonseam_rets", "rp_wrap_nonseam_wrapped", "rp_outside_args",
    "rp_outside_rets", "rp_outside_wrapped", "mut_borrow_wrapper",
    "field_proj_wrapped", "field_proj_outside_impl", "void_ptr_sanctioned",
    "void_ptr_smell", "raw_ptr_derefs", "total_stmts", "code_lines",
)
_GLOBAL_SITES = ("raw_ptr_sites", "void_ptr_sites", "field_proj_sites")


def _merge_globals(globals_):
    """Sum the count fields and concatenate the site arrays across per-crate
    global emissions into one tree-wide `global` section."""
    out = {k: 0 for k in _GLOBAL_COUNTS}
    sites = {k: [] for k in _GLOBAL_SITES}
    for g in globals_:
        for k in _GLOBAL_COUNTS:
            out[k] += int(g.get(k, 0))
        for k in _GLOBAL_SITES:
            sites[k].extend(g.get(k, []))
    out.update(sites)
    return out


def _audit_hir(target, sel, *, all, names, crate, mod, file):
    """Resolution-aware backend: drive the `unsafe_metrics` rustc pass over the
    workspace (seed mode, which also emits the global block), then merge.

    Requires a *buildable* workspace (it is a real rustc front end). On a build
    failure it errors with a hint rather than silently degrading."""
    import os
    import subprocess
    from crustify.layout import Layout

    ws = Layout.discover(target).rust
    if not ws.is_dir():
        raise SystemExit(f"error: no Rust tree at {ws}. Run `scaffold` first.")

    driver_dir = _CRUSTIFY_ROOT / "utils" / "unsafe_metrics"
    driver_bin = driver_dir / "target" / "debug" / "unsafe_metrics"
    # Build the driver (cached after the first time).
    b = subprocess.run(["cargo", "+nightly", "build"], cwd=driver_dir,
                       capture_output=True, text=True)
    if b.returncode != 0:
        raise SystemExit("audit: failed to build the HIR driver "
                         f"(utils/unsafe_metrics):\n{b.stderr[-1500:]}")
    sysroot = subprocess.run(["rustc", "+nightly", "--print", "sysroot"],
                             capture_output=True, text=True).stdout.strip()

    # The driver only emits during compilation; cargo skips cached crates and
    # `UM_MODE` is not in its fingerprint, so bust the workspace crates' roots
    # to force re-emission on every audit (deps stay cached).
    for root in list(ws.glob("*/src/lib.rs")) + list(ws.glob("*/src/main.rs")):
        root.touch()

    env = dict(os.environ)
    env["UM_MODE"] = "seed"
    env["RUSTC"] = str(driver_bin)
    env["SYSROOT"] = sysroot
    ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{sysroot}/lib" + (f":{ld}" if ld else "")
    if names:
        env["UM_SEED_NAME"] = " ".join(names)
    elif file:
        env["UM_SEED_FILE"] = file
    elif mod:
        env["UM_SEED_DIR"] = mod
    else:                      # --all or --crate
        env["UM_SEED_ALL"] = "1"

    cmd = ["cargo", "+nightly", "build"]
    if crate:
        cmd += ["-p", crate]
    r = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "audit: the Rust workspace failed to compile under the HIR auditor.\n"
            "The HIR backend is a real rustc front end and needs a buildable\n"
            "crate (run after `cargo check` is green, or pass --engine regex).\n"
            f"{r.stderr[-1800:]}")

    entries, globals_ = [], []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and '"crate"' in line):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `-sys` crates are generated FFI bindings (extern declarations), never
        # the port subject: a seed name also resolves to the binding there,
        # emitting an empty noise entry. Skip them on both seed and global lines.
        if d.get("crate", "").endswith("_sys"):
            continue
        if "seeds" in d:
            for s in d["seeds"]:
                s["crate"] = d["crate"]
                entries.append(s)
        elif "unsafe_blocks" in d:
            globals_.append(d)

    if not entries and not (all or crate):
        raise SystemExit(f"audit: no types/symbols matched {sel}.")
    doc = {"seed": sel, "entries": entries, "global": _merge_globals(globals_)}
    print(json.dumps(doc, indent=2))
