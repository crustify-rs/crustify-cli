#!/usr/bin/env python3
"""One-shot: lift the authored half out of the per-stem analysis tree into
`crustify/ownership-store.json`.

Reads every `types.json` / `syms.json` under `<repo>/crustify/analysis/`, keeps
only the keys no composer can derive, and writes the single store. The stem
tree is left alone -- deleting it is a separate, deliberate step, after the
read path has been verified against it.

    python3 utils/migrate_ownership_store.py <repo_root> [--write]

Without `--write` it reports what it would extract and exits, so the counts can
be checked against the tree first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crustify import store as S          # noqa: E402
from crustify.layout import Layout       # noqa: E402


# Keys the TYPE composer emits on a field: `ptr` among them, as a null
# skeleton on every pointer field. So `ptr` is stored only when the agent
# actually filled it -- carrying 1676 null skeletons would put the derivable
# half straight back into the store.
#
# `refcount` / `locked_by` / `_comment_agent` the composer NEVER emits, so
# their mere PRESENCE is the agent's answer and the value is immaterial:
# `refcount: false` on a visited type means "I checked, this field is not the
# refcount", which is a finding, not an absence. Filtering on truthiness lost
# 55 refcount and 67 locked_by judgements and kept only the 11 + 6 positive
# ones.
_FIELD_PRESENCE_KEYS = ("refcount", "locked_by", "_comment_agent")


def _filled_ptr(ptr) -> bool:
    return isinstance(ptr, dict) and any(
        v not in (None, False) for k, v in ptr.items() if k != "note")


def _kept_field(f: dict) -> dict | None:
    """The stored form of one field, or None when the agent wrote nothing."""
    out: dict = {}
    if _filled_ptr(f.get("ptr")):
        out["ptr"] = f["ptr"]
    for k in _FIELD_PRESENCE_KEYS:
        if k in f:
            out[k] = f[k]
    return {"name": f["name"], **out} if out else None


def extract(analysis: Path) -> dict:
    doc = S.empty()
    for p in sorted(analysis.rglob("types.json")):
        for e in json.load(open(p)).get("types") or []:
            fields = [x for x in
                      (_kept_field(f) for f in (e.get("fields") or [])
                       if isinstance(f, dict) and f.get("name"))
                      if x]
            if not fields and not e.get("_comment_agent"):
                continue
            rec = {"name": e.get("name") or e.get("type"),
                   "defined_in": e.get("defined_in")}
            if e.get("_comment_agent"):
                rec["_comment_agent"] = e["_comment_agent"]
            if fields:
                rec["fields"] = fields
            doc["types"].append(rec)

    for p in sorted(analysis.rglob("syms.json")):
        for e in json.load(open(p)).get("symbols") or []:
            body: dict = {}
            # `lifetime` / `locked_by` / `ptr` are composer-seeded as null on
            # every symbol, so only a non-null value is the agent's; `forks`
            # and `_comment_agent` the composer never emits, so presence wins.
            for k in ("lifetime", "locked_by", "ptr"):
                if e.get(k) not in (None, False, [], {}):
                    body[k] = e[k]
            for k in ("forks", "_comment_agent"):
                if k in e:
                    body[k] = e[k]
            args = [{"name": a["name"], "ptr": a["ptr"]}
                    for a in (e.get("ptr_args") or [])
                    if isinstance(a, dict) and _filled_ptr(a.get("ptr"))
                    and a.get("name")]
            if args:
                body["ptr_args"] = args
            ret = e.get("ptr_ret")
            if isinstance(ret, dict) and _filled_ptr(ret.get("ptr")):
                body["ptr_ret"] = {"ptr": ret["ptr"]}
            # A fork's `used_by.call` is the agent's -- the invokers that
            # realize this variant's contract. The composer emits none of it
            # (it has one declaration, not two), so it is authored data.
            if e.get("variant"):
                calls = (e.get("used_by") or {}).get("call")
                if calls:
                    body["callsites"] = list(calls)
            if not body:
                continue
            rec = {"name": e["name"], "defined_in": e.get("defined_in")}
            if e.get("variant"):
                rec["variant"] = e["variant"]
            doc["symbols"].append({**rec, **body})

    return S.normalize(doc)


def verify(analysis: Path, doc: dict) -> list[str]:
    """Every authored datum in the tree must appear in the store. Compares the
    VALUES, not just the counts -- a migration that silently re-shaped a `ptr`
    block would pass a count check."""
    ti, si = S.index(doc)
    errs: list[str] = []
    for p in sorted(analysis.rglob("types.json")):
        for e in json.load(open(p)).get("types") or []:
            for f in (e.get("fields") or []):
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                want = _kept_field(f)
                if not want:
                    continue
                rec = ti.get((e.get("name") or e.get("type"),
                              e.get("defined_in") or ""))
                got = next((x for x in (rec or {}).get("fields") or []
                            if x.get("name") == f["name"]), None)
                if got != want:
                    errs.append(f"type {e.get('name')}.{f['name']}")
    for p in sorted(analysis.rglob("syms.json")):
        for e in json.load(open(p)).get("symbols") or []:
            rec = si.get((e["name"], e.get("defined_in") or "",
                          e.get("variant") or 0))
            for k in ("lifetime", "locked_by", "ptr"):
                v = e.get(k)
                if v not in (None, False, [], {}) and (rec or {}).get(k) != v:
                    errs.append(f"sym {e['name']}.{k}")
            for k in ("forks", "_comment_agent"):
                if k in e and (rec or {}).get(k) != e[k]:
                    errs.append(f"sym {e['name']}.{k}")
            if e.get("variant"):
                calls = (e.get("used_by") or {}).get("call") or []
                if calls and (rec or {}).get("callsites") != list(calls):
                    errs.append(f"sym {e['name']} v{e['variant']} callsites")
            for a in (e.get("ptr_args") or []):
                if not (isinstance(a, dict) and _filled_ptr(a.get("ptr"))
                        and a.get("name")):
                    continue
                got = next((x for x in (rec or {}).get("ptr_args") or []
                            if x.get("name") == a["name"]), None)
                if not got or got.get("ptr") != a["ptr"]:
                    errs.append(f"sym {e['name']} arg {a['name']}")
            ret = e.get("ptr_ret")
            if isinstance(ret, dict) and _filled_ptr(ret.get("ptr")):
                if ((rec or {}).get("ptr_ret") or {}).get("ptr") != ret["ptr"]:
                    errs.append(f"sym {e['name']} ptr_ret")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    layout = Layout.discover(Path(args.repo_root))
    doc = extract(layout.analysis)
    errs = verify(layout.analysis, doc)

    blob = json.dumps(doc, indent=1) + "\n"
    print(f"[migrate] {len(doc['types'])} type records / "
          f"{len(doc['symbols'])} symbol records — {len(blob)/1024:.1f} KB")
    if errs:
        print(f"[migrate] VERIFY FAILED on {len(errs)} datum(s):", file=sys.stderr)
        for e in errs[:20]:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("[migrate] verify: every authored datum round-trips")
    if args.write:
        S.path(layout).write_text(blob)
        print(f"[migrate] wrote {S.path(layout)}")
    else:
        print("[migrate] dry run — pass --write to install")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
