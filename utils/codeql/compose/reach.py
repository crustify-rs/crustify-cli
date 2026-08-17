"""Reach composer — joins T2 edge CSVs with the T3 scope predicate
and exposes per-entity reach-from-port queries to the downstream
manifest composers.

The reach problem decomposes into one rollup per edge kind:

  function_calls       → callers / callees indexed both directions
  function_addresses   → addr-of-targets indexed both directions
  global_accesses      → accessors per global (port side)
  macro_expansions     → expansion sites per macro
  field_accesses       → per-struct field touch sites
  signature_type_uses  → per-function signature type tags

Each rollup needs to answer two kinds of question:

  - "For this WRAP entity, which PORT-SIDE sites reach it?"
    → drives import-section manifest inclusion + `called_by`/`ref`
      population on wrap entries.
  - "For this PORT entity, which sites (port + wrap) reach it,
    and what does it itself reach?"
    → drives target-section manifest's `called_by` (inverse) +
      `depends_on` (forward).

The class builds all indexes up front from the T2 CSVs, then exposes
query methods. Manifest composers instantiate one `Reach` and call
its methods per entity.

Scope partitioning is delegated to the imported `scope.classify(...)`
function so the rule stays in one place.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from . import scope

# Typedefs whose unaliased base is an anonymous aggregate — `typedef struct
# { … } T;` — have no tagged terminal, so their uses can only be re-keyed under
# the typedef's own declared identity (see `_inverse_type_keys`).
_ANON_AGGREGATE_KINDS = {"struct_anonymous", "union_anonymous", "enum_anonymous"}


def _load_csv(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


class Reach:
    """In-memory rollup of all T2 edge CSVs against a port-path set.

    Built once per composer run. Methods are read-only after init.
    """

    def __init__(
        self,
        csv_dir_t2: Path,
        port_paths: set[str],
        *,
        csv_dir_t1: Path | None = None,
    ) -> None:
        """Build all per-edge / per-entity reach indexes.

        `csv_dir_t2` carries the T2 edge CSVs (function_calls,
        macro_expansions, field_accesses, etc.). `csv_dir_t1`
        carries the T1 entity CSVs and is optional: when provided,
        the `fields.csv` entity row is loaded so consumers can
        look up per-field `(field_type, is_scalar)` metadata via
        `field_type_of()`. The split keeps the constructor honest
        about which directory each CSV comes from — consumers
        that don't need the field-type lookup can pass only the T2
        path.
        """
        self.port_paths = port_paths
        # Typedef resolver (optional, from the T1 types.csv). Lets the
        # type-use INVERSE indexes additionally key each use under its
        # terminal struct/union/enum identity, so a use written against a
        # typedef — which CodeQL records with type_def_file="" — is still
        # found when a consumer queries by the struct's (tag, def_file).
        # Built BEFORE the type-use index builders, which consume it.
        self._by_name: dict[str, dict] = {}
        if csv_dir_t1 is not None:
            self._by_name = scope.build_types_index(
                scope.load_csv(csv_dir_t1 / "types.csv")
            )
        self._build_function_call_indexes(csv_dir_t2 / "function_calls.csv")
        self._build_function_address_indexes(csv_dir_t2 / "function_addresses.csv")
        self._build_global_access_indexes(csv_dir_t2 / "global_accesses.csv")
        self._build_macro_expansion_indexes(csv_dir_t2 / "macro_expansions.csv")
        self._build_field_access_indexes(csv_dir_t2)
        self._build_signature_type_indexes(csv_dir_t2 / "signature_type_uses.csv")
        self._build_callback_sig_type_indexes(csv_dir_t2 / "callback_signature_type_uses.csv")
        self._build_callback_callsite_indexes(csv_dir_t2 / "callback_call_sites.csv")
        self._build_local_type_indexes(csv_dir_t2 / "local_type_uses.csv")
        self._build_field_type_use_indexes(csv_dir_t2 / "field_type_uses.csv")
        self._build_global_type_use_indexes(csv_dir_t2 / "global_type_uses.csv")
        self._build_function_ptr_arg_indexes(csv_dir_t2 / "function_pointer_args.csv")
        self._build_function_ptr_return_indexes(csv_dir_t2 / "function_pointer_returns.csv")
        self._build_cast_indexes(csv_dir_t2 / "casts.csv")
        # T1-side entity CSV — fields metadata. Optional.
        if csv_dir_t1 is not None:
            self._build_field_metadata_indexes(csv_dir_t1 / "fields.csv")
        else:
            self._field_metadata: dict[tuple[str, str, str], tuple[str, bool]] = {}

    # ------------------------------------------------------------ typedef identity

    def _terminal_struct_key(
        self, type_name: str, type_kind: str,
    ) -> tuple[str, str] | None:
        """For a typedef use, the terminal struct/union/enum identity
        `(tag, def_file)`. Returns None for non-typedefs, scalar/primitive
        typedefs, anonymous bases, or when no T1 resolver is loaded — those
        keep only their own key (non-scalar resolution only)."""
        if type_kind != "typedef" or not self._by_name:
            return None
        terminal = scope.resolve_typedef(type_name, self._by_name)
        if (terminal is not None
                and terminal["kind"] in {"struct", "union", "enum"}
                and not terminal["name"].startswith("(")):
            return (terminal["name"], terminal["def_file"] or "")
        return None

    def _inverse_type_keys(
        self, type_name: str, type_def_file: str, type_kind: str,
    ) -> list[tuple[str, str]]:
        """Keys under which to file a type-use in an inverse index: the
        use's own `(name, def_file)` plus, for a non-scalar typedef, its
        terminal struct identity.

        Anonymous-aggregate typedefs (`typedef struct { … } T;`) have no
        tagged terminal to promote to, so `_terminal_struct_key` yields
        nothing and CodeQL records the use with `def_file=""` — leaving the
        use reachable only by a name-only query. File it ALSO under the
        typedef's own declared identity `(name, its-T1-def_file)` so a
        consumer querying by `(name, header)` resolves it. Unlike a bare
        `(name, "")` key this stays disambiguated by def_file when two files
        declare a same-named anonymous typedef (cf. the `struct entry`
        collision the type composers guard against)."""
        keys = [(type_name, type_def_file)]
        term = self._terminal_struct_key(type_name, type_kind)
        if term is not None and term != (type_name, type_def_file):
            keys.append(term)
        elif type_kind == "typedef" and not type_def_file:
            own = self._by_name.get(type_name)
            if (own and own.get("unaliased_kind") in _ANON_AGGREGATE_KINDS
                    and own.get("def_file")):
                extra = (type_name, own["def_file"])
                if extra != (type_name, type_def_file):
                    keys.append(extra)
        return keys

    # ------------------------------------------------------------ port test

    def _is_port(self, path: str) -> bool:
        return path in self.port_paths

    # ------------------------------------------------------------ function_calls

    def _build_function_call_indexes(self, path: Path) -> None:
        # Forward: keyed by (caller_name, caller_file) → set of (callee_name, callee_def_file).
        # Inverse: keyed by (callee_name, callee_def_file) → set of (caller_name, caller_file).
        self._fc_forward: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self._fc_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for r in _load_csv(path):
            caller = (r["caller_name"], r["caller_file"])
            callee = (r["callee_name"], r["callee_def_file"])
            self._fc_forward[caller].add(callee)
            self._fc_inverse[callee].add(caller)

    # ------------------------------------------------------------ function_addresses

    def _build_function_address_indexes(self, path: Path) -> None:
        # Forward: (enclosing_name, access_file) → set of (target_name, target_def_file).
        # Inverse: (target_name, target_def_file) → set of (enclosing_name, access_file).
        # `enclosing_name` may be "" for file-scope addr-of (static initializers).
        self._fa_forward: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self._fa_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for r in _load_csv(path):
            site = (r["enclosing_name"], r["access_file"])
            target = (r["target_name"], r["target_def_file"])
            self._fa_forward[site].add(target)
            self._fa_inverse[target].add(site)

    # ------------------------------------------------------------ global_accesses

    def _build_global_access_indexes(self, path: Path) -> None:
        # Forward: (enclosing_name, access_file) → set of (global_name, global_def_file, access_kind).
        # Inverse: (global_name, global_def_file) → set of (enclosing_name, access_file, access_kind).
        # We keep access_kind on the edge so consumers can attribute
        # read vs write vs addr per accessor if needed.
        self._ga_forward: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
        self._ga_inverse: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
        for r in _load_csv(path):
            site = (r["enclosing_name"], r["access_file"])
            glob = (r["global_name"], r["global_def_file"])
            kind = r["access_kind"]
            self._ga_forward[site].add(glob + (kind,))
            self._ga_inverse[glob].add((r["enclosing_name"], r["access_file"], kind))

    # ------------------------------------------------------------ macro_expansions

    def _build_macro_expansion_indexes(self, path: Path) -> None:
        # Forward: keyed by (enclosing_name, invocation_file) → set of (macro_name, macro_def_file).
        #   For file-scope expansions, enclosing_name="" and the key
        #   represents the invocation_file. This is fine — both fn
        #   bodies and file-scope expansions are valid "who expands
        #   this macro" sources for the consumer.
        # Inverse: (macro_name, macro_def_file) → set of
        #   (enclosing_name, invocation_file).
        self._me_forward: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self._me_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for r in _load_csv(path):
            site = (r["enclosing_name"], r["invocation_file"])
            macro = (r["macro_name"], r["macro_def_file"])
            self._me_forward[site].add(macro)
            self._me_inverse[macro].add(site)

    # ------------------------------------------------------------ field_accesses

    def _build_field_access_indexes(self, csv_dir_t2: Path) -> None:
        # Forward: (enclosing_name, access_file) → set of (struct_name, struct_def_file, field_name, access_kind).
        # Per-struct: (struct_name, struct_def_file) → list of
        #   (enclosing_name, access_file, field_name, access_kind).
        # The per-struct index is what types_manifest.py uses to
        # populate fields[] and non_opaque_in.
        #
        # Source is `fa_with_root.csv`, NOT `field_accesses.csv`: an access
        # through an ANONYMOUS embedded member (`s->ext.hostname`) carries
        # `struct_name = "(unnamed class/struct/union)"`, which every consumer
        # filters out — so those accesses used to vanish from both `fields[]`
        # and `non_opaque_in`. `fa_with_root` walks the qualifier chain to the
        # outermost NAMED container and supplies the dotted `field_path`
        # (`ext.hostname`), matching the qualified names `entities/fields.ql`
        # emits. Falls back to the flat CSV when the enriched one is absent
        # (a pre-existing extraction).
        path = csv_dir_t2 / "fa_with_root.csv"
        rooted = path.is_file()
        if not rooted:
            path = csv_dir_t2 / "field_accesses.csv"
        self._fda_forward: dict[tuple[str, str], set[tuple[str, str, str, str]]] = defaultdict(set)
        self._fda_by_struct: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
        for r in _load_csv(path):
            site = (r["enclosing_name"], r["access_file"])
            if rooted and r.get("root_struct_name"):
                struct = (r["root_struct_name"], r.get("root_struct_def_file", ""))
                field = r.get("field_path") or r["field_name"]
            else:
                struct = (r["struct_name"], r["struct_def_file"])
                field = r["field_name"]
            kind = r["access_kind"]
            self._fda_forward[site].add(struct + (field, kind))
            self._fda_by_struct[struct].append(
                (r["enclosing_name"], r["access_file"], field, kind)
            )

    # ------------------------------------------------------------ signature_type_uses

    def _build_signature_type_indexes(self, path: Path) -> None:
        # Forward: (function_name, function_def_file) → list of
        #   (type_name, type_kind, type_def_file, position).
        # Inverse: (type_name, type_def_file) → set of
        #   (function_name, function_def_file).
        self._stu_forward: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
        self._stu_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for r in _load_csv(path):
            fn = (r["function_name"], r["function_def_file"])
            self._stu_forward[fn].append(
                (r["type_name"], r["type_kind"], r["type_def_file"], r["position"])
            )
            for ty in self._inverse_type_keys(
                r["type_name"], r["type_def_file"], r["type_kind"]
            ):
                self._stu_inverse[ty].add(fn)

    def _build_cast_indexes(self, path: Path) -> None:
        # Raw struct<->struct cast graph (edges/casts.ql). Keyed by tag:
        #   forward: from_tag -> set of to_tags  (the type's `casted.to`)
        #   inverse: to_tag   -> set of from_tags (the type's `casted.from`)
        # No classification — engine erasure, downcast and ASN1
        # ITEM punning all coexist here; consumers disambiguate.
        self._cast_to: dict[str, set[str]] = defaultdict(set)
        self._cast_from: dict[str, set[str]] = defaultdict(set)
        if not path.exists():
            return
        for r in _load_csv(path):
            f, t = r.get("from_tag"), r.get("to_tag")
            if f and t:
                self._cast_to[f].add(t)
                self._cast_from[t].add(f)

    def _build_callback_sig_type_indexes(self, path: Path) -> None:
        # Forward only: (callback_name, callback_def_file) → list of
        #   (type_name, type_kind, type_def_file, position).
        # Deliberately NOT folded into `_stu_inverse` — a callback typedef is
        # not a function "using" its arg types in the consumer sense, so it must
        # not pollute `functions_using_type` (op-candidate / consumer-footprint
        # logic). The callback's own `used_by` comes from the function-side
        # `signature_type_uses` inverse instead.
        self._cbstu_forward: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
        if not path.exists():
            return
        for r in _load_csv(path):
            cb = (r["callback_name"], r["callback_def_file"])
            self._cbstu_forward[cb].append(
                (r["type_name"], r["type_kind"], r["type_def_file"], r["position"])
            )

    def _build_callback_callsite_indexes(self, path: Path) -> None:
        # (callback_name, callback_def_file) → set of callsite function names.
        # The invocation sites (functions that actually CALL the callback), a
        # refinement of `used_by` that excludes pass-through declarations —
        # where the arg/return borrow-vs-own contract is realised.
        self._cb_callsites: dict[tuple[str, str], set[str]] = defaultdict(set)
        # The same relation FORWARD: (callsite_name, callsite_def_file) → the
        # callback names that function invokes. An indirect call through a
        # function pointer is a call, so this feeds the invoker's
        # `depends_on.syms` exactly like `callees_of` — a callback reached
        # through a struct field is named nowhere else in the invoker's record
        # (its `ptr_args` renders the pointee as `"(routine)"`, and the owning
        # struct's `fields[]` is only the target-accessed subset).
        self._cb_invoked: dict[tuple[str, str], set[str]] = defaultdict(set)
        if not path.exists():
            return
        for r in _load_csv(path):
            cb = (r["callback_name"], r["callback_def_file"])
            if r["callsite_name"]:
                self._cb_callsites[cb].add(r["callsite_name"])
                self._cb_invoked[
                    (r["callsite_name"], r["callsite_def_file"])
                ].add(r["callback_name"])

    # ================================================================ Query API

    # ---------------- function-side queries

    def port_callers_of(self, callee_name: str, callee_def_file: str) -> set[str]:
        """Port-side function names that CALL the given function.
        Used to populate `called_by.call` for import-section and
        target function entries alike.
        """
        return {
            caller for caller, caller_file in self._fc_inverse.get((callee_name, callee_def_file), set())
            if self._is_port(caller_file)
        }

    def all_callers_of(self, callee_name: str, callee_def_file: str) -> set[str]:
        """Any-scope function names that CALL the given function.
        Used by target-section `called_by.call` since the port manifest
        is an inventory — callers may be port or wrap.
        """
        return {caller for caller, _ in self._fc_inverse.get((callee_name, callee_def_file), set())}

    def port_referrers_of(self, target_name: str, target_def_file: str) -> set[str]:
        """Port-side enclosing function names that take this
        function's ADDRESS (callback storage, &fn, initializer
        entries). Drives `called_by.ref` on wrap function entries.

        Dedup against `port_callers_of` to enforce the
        "site in call → not in ref" rule documented in the symbol
        wrapper prompt.
        """
        return {
            enc for enc, access_file in self._fa_inverse.get((target_name, target_def_file), set())
            if enc and self._is_port(access_file)
        }

    def all_referrers_of(self, target_name: str, target_def_file: str) -> set[str]:
        """Any-scope enclosing function names that take this
        function's address. Port-scope inventory uses this for the
        full ref set.
        """
        return {enc for enc, _ in self._fa_inverse.get((target_name, target_def_file), set()) if enc}

    def is_function_port_reachable(self, callee_name: str, callee_def_file: str) -> bool:
        """True iff this function is called OR addr-taken from any
        target-side site. Wrap manifest inclusion gate for functions.
        """
        if self.port_callers_of(callee_name, callee_def_file):
            return True
        if self.port_referrers_of(callee_name, callee_def_file):
            return True
        return False

    # ---------------- global-side queries

    def port_accessors_of(self, global_name: str, global_def_file: str) -> set[str]:
        """Port-side enclosing function names that READ / WRITE /
        TAKE THE ADDRESS of the given global. Unified per the
        `called_by.ref` rule for globals — kind not split here.
        """
        return {
            enc for enc, access_file, _ in self._ga_inverse.get((global_name, global_def_file), set())
            if enc and self._is_port(access_file)
        }

    def all_accessors_of(self, global_name: str, global_def_file: str) -> set[str]:
        return {enc for enc, _, _ in self._ga_inverse.get((global_name, global_def_file), set()) if enc}

    def is_global_port_reachable(self, global_name: str, global_def_file: str) -> bool:
        return bool(self.port_accessors_of(global_name, global_def_file))

    # ---------------- macro-side queries

    def port_macro_call_sites(self, macro_name: str, macro_def_file: str) -> set[str]:
        """Port-side ENCLOSING FUNCTION names that expand this macro
        from a function body. Drives `called_by.call` for
        `macro_symbol` kind (and historically for `macro_constant` /
        `macro_misc` `called_by.ref` — disambiguation belongs to the
        agent based on macro kind, not here).
        """
        return {
            enc for enc, inv_file in self._me_inverse.get((macro_name, macro_def_file), set())
            if enc and self._is_port(inv_file)
        }

    def port_macro_file_sites(self, macro_name: str, macro_def_file: str) -> set[str]:
        """Port-side FILE PATHS that contain a file-scope expansion
        of this macro. Drives `called_by.call` for a macro whose
        expansion lands at file scope rather than inside a function
        body.
        """
        return {
            inv_file for enc, inv_file in self._me_inverse.get((macro_name, macro_def_file), set())
            if not enc and self._is_port(inv_file)
        }

    def is_macro_port_reachable(self, macro_name: str, macro_def_file: str) -> bool:
        return bool(
            self.port_macro_call_sites(macro_name, macro_def_file)
            or self.port_macro_file_sites(macro_name, macro_def_file)
        )

    def all_macro_call_sites(self, macro_name: str, macro_def_file: str) -> set[str]:
        return {enc for enc, _ in self._me_inverse.get((macro_name, macro_def_file), set()) if enc}

    def all_macro_file_sites(self, macro_name: str, macro_def_file: str) -> set[str]:
        return {inv_file for enc, inv_file in self._me_inverse.get((macro_name, macro_def_file), set()) if not enc}

    # ------------------------------------------------------------ field_type_uses (T2)

    def _build_field_type_use_indexes(self, path: Path) -> None:
        """Load `edges/field_type_uses.csv` — for each (struct, field),
        the list of user types referenced in the field's declared type.

        Forward index: (struct_name, struct_def_file, field_name) →
        set of (type_name, type_kind, type_def_file).
        Inverse index: (type_name, type_def_file) → set of
        (struct_name, struct_def_file, field_name).

        Both keep the same shape — the inverse lookup is what
        `types_manifest.py` uses to ask "which (struct, field) pairs
        carry type T?", joined against target-side field_accesses.
        """
        self._ftu_forward: dict[tuple[str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
        self._ftu_inverse: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
        if not path.exists():
            return
        for r in _load_csv(path):
            key = (r["struct_name"], r["struct_def_file"], r["field_name"])
            self._ftu_forward[key].add(
                (r["type_name"], r["type_kind"], r["type_def_file"])
            )
            for ty in self._inverse_type_keys(
                r["type_name"], r["type_def_file"], r["type_kind"]
            ):
                self._ftu_inverse[ty].add(key)

    def types_in_field(
        self, struct_name: str, struct_def_file: str, field_name: str
    ) -> list[tuple[str, str, str]]:
        """User types referenced in the type of the given field.
        Returns list of (type_name, type_kind, type_def_file).
        Empty when the field isn't in the index (anonymous declaring
        struct, or `field_type_uses.csv` not loaded).
        """
        return list(self._ftu_forward.get((struct_name, struct_def_file, field_name), set()))

    def fields_referencing_type(
        self, type_name: str, type_def_file: str
    ) -> set[tuple[str, str, str]]:
        """Inverse: (struct, field) entries whose type references the
        given user type. Drives the field-driven port-reachability
        gate — joined against target-side `field_accesses` to find
        types transitively reached via a target-touched field.
        """
        return set(self._ftu_inverse.get((type_name, type_def_file), set()))

    def port_touched_fields(self) -> set[tuple[str, str, str]]:
        """Every (struct, field) tuple that ANY target-side field
        access references. Built lazily from the existing
        field_accesses index — no separate CSV needed. Used by the
        composer's `_wrap_port_reachable` to enumerate
        target-touched fields for the field-type join.
        """
        if not hasattr(self, "_port_field_cache"):
            self._port_field_cache: set[tuple[str, str, str]] = set()
            for (struct_name, struct_def_file), records in self._fda_by_struct.items():
                for enc, access_file, field, _kind in records:
                    if self._is_port(access_file):
                        self._port_field_cache.add(
                            (struct_name, struct_def_file, field)
                        )
        return self._port_field_cache

    # ------------------------------------------------------------ global_type_uses (T2)

    def _build_global_type_use_indexes(self, path: Path) -> None:
        """Load `edges/global_type_uses.csv` — for each global, the
        list of user types referenced in its declared type.

        Mirrors the field_type_uses index shape but keyed on
        (global_name, global_def_file).
        """
        self._gtu_forward: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
        self._gtu_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        if not path.exists():
            return
        for r in _load_csv(path):
            key = (r["global_name"], r["global_def_file"])
            self._gtu_forward[key].add(
                (r["type_name"], r["type_kind"], r["type_def_file"])
            )
            for ty in self._inverse_type_keys(
                r["type_name"], r["type_def_file"], r["type_kind"]
            ):
                self._gtu_inverse[ty].add(key)

    def types_in_global_type(
        self, global_name: str, global_def_file: str
    ) -> list[tuple[str, str, str]]:
        """User types referenced in the given global's declared type."""
        return list(self._gtu_forward.get((global_name, global_def_file), set()))

    def globals_referencing_type(
        self, type_name: str, type_def_file: str
    ) -> set[tuple[str, str]]:
        """Inverse: globals whose declared type references the given
        user type.
        """
        return set(self._gtu_inverse.get((type_name, type_def_file), set()))

    def port_accessed_globals(self) -> set[tuple[str, str]]:
        """Every (global_name, global_def_file) that any target-side
        access touches. Built lazily from the existing
        global_accesses index. Drives scenario 7 of the reach
        ruleset.
        """
        if not hasattr(self, "_port_global_cache"):
            self._port_global_cache: set[tuple[str, str]] = set()
            for (glob_name, glob_def), accessors in self._ga_inverse.items():
                for _enc, access_file, _kind in accessors:
                    if self._is_port(access_file):
                        self._port_global_cache.add((glob_name, glob_def))
                        break
        return self._port_global_cache

    # ------------------------------------------------------------ function_pointer_args (T2)

    def _build_function_ptr_arg_indexes(self, path: Path) -> None:
        """Load `edges/function_pointer_args.csv` — one row per
        (function, ptr-arg-position). Index by (function_name,
        function_def_file) → ordered list of arg dicts.

        Each arg dict carries the composer-fillable fields the
        syms-manifest populates verbatim onto `ptr_args[]`:
        position, name, type, const (bool), depth.
        """
        self._fpa_by_fn: dict[tuple[str, str], list[dict]] = defaultdict(list)
        if not path.exists():
            return
        rows: list[tuple[tuple[str, str], int, dict]] = []
        for r in _load_csv(path):
            key = (r["function_name"], r["function_def_file"])
            pos = int(r["position"])
            rows.append((key, pos, {
                "position": pos,
                "name": r["param_name"] or f"arg{pos}",
                "type": r["pointee_type"],
                "const": r["is_const"] == "true",
                "depth": int(r["depth"]),
            }))
        # Sort by position within each function so consumers get a
        # stable ordering matching the C signature.
        rows.sort(key=lambda x: (x[0], x[1]))
        for key, _pos, arg in rows:
            self._fpa_by_fn[key].append(arg)

    def ptr_args_of(self, fn_name: str, fn_def_file: str) -> list[dict]:
        """Composer-fillable `ptr_args[]` entries for the given
        function. Empty list when the function has no pointer args.
        """
        return list(self._fpa_by_fn.get((fn_name, fn_def_file), []))

    # ------------------------------------------------------------ function_pointer_returns (T2)

    def _build_function_ptr_return_indexes(self, path: Path) -> None:
        """Load `edges/function_pointer_returns.csv` — one row per
        function with pointer return. Index by (function_name,
        function_def_file) → single ret dict.
        """
        self._fpr_by_fn: dict[tuple[str, str], dict] = {}
        if not path.exists():
            return
        for r in _load_csv(path):
            key = (r["function_name"], r["function_def_file"])
            self._fpr_by_fn[key] = {
                "type": r["pointee_type"],
                "const": r["is_const"] == "true",
                "depth": int(r["depth"]),
            }

    def ptr_ret_of(self, fn_name: str, fn_def_file: str) -> dict | None:
        """Composer-fillable `ptr_ret` dict for the given function,
        or `None` when the function's return type isn't a pointer.
        """
        ret = self._fpr_by_fn.get((fn_name, fn_def_file))
        return dict(ret) if ret is not None else None

    # ------------------------------------------------------------ field metadata (T1)

    def _build_field_metadata_indexes(self, path: Path) -> None:
        """Load `entities/fields.csv` into two indexes:

          - `(struct, field) → (field_type, is_scalar, ptr_depth)` point
            lookup (`field_type_of`).
          - `(struct, def_file) → [(field, field_type, is_scalar, ptr_depth)]`
            ordered list of ALL declared fields (`struct_fields`),
            used to compose the full layout for target types.

        `is_scalar` is parsed to bool at load time; `ptr_depth` to int
        (0 when the column is absent — a pre-`ptr_depth` extraction). The list preserves
        the CSV row order (CodeQL `Field` iteration order), which is
        not guaranteed to match declaration order — layout-faithful
        ordering needs a fields.ql ordinal column (see docs/TODO.md).
        """
        self._field_metadata: dict[tuple[str, str, str], tuple[str, bool, int]] = {}
        self._struct_field_list: dict[tuple[str, str], list[tuple[str, str, bool, int]]] = defaultdict(list)
        if not path.exists():
            return
        for r in _load_csv(path):
            scalar = r["is_scalar"] == "true"
            depth = int(r.get("ptr_depth") or 0)
            self._field_metadata[(r["struct_name"], r["struct_def_file"], r["field_name"])] = (
                r["field_type"], scalar, depth,
            )
            self._struct_field_list[(r["struct_name"], r["struct_def_file"])].append(
                (r["field_name"], r["field_type"], scalar, depth)
            )

    def field_type_of(
        self, struct_name: str, struct_def_file: str, field_name: str
    ) -> tuple[str, bool, int] | None:
        """Return (field_type_string, is_scalar, ptr_depth) for the given
        (struct, field). None when the field isn't in the
        entities/fields.csv index — either fields.csv wasn't
        provided at construction, or the declaring struct is
        anonymous (those rows are filtered by entities/fields.ql).
        """
        return self._field_metadata.get((struct_name, struct_def_file, field_name))

    def struct_fields(
        self, struct_name: str, struct_def_file: str
    ) -> list[tuple[str, str, bool, int]]:
        """All declared fields of a struct as ordered
        `(field_name, field_type, is_scalar, ptr_depth)` tuples. Empty when the
        struct isn't in fields.csv (no full-body definition, or
        anonymous declaring type). Used to compose the full field
        layout for target types (vs the access-narrowed subset
        used for import types).
        """
        return list(self._struct_field_list.get((struct_name, struct_def_file), []))

    # ---------------- struct field-access queries

    def field_access_records(self, struct_name: str, struct_def_file: str) -> list[tuple[str, str, str, str]]:
        """All (enclosing_name, access_file, field_name, access_kind)
        records for this struct. Consumers filter port vs wrap and
        roll up per-field summaries themselves.
        """
        return list(self._fda_by_struct.get((struct_name, struct_def_file), []))

    def port_field_access_files(self, struct_name: str, struct_def_file: str) -> set[str]:
        """Port-side access_file paths that touch any field of this
        struct. Drives `non_opaque_in` on type-manifest entries.
        """
        return {
            access_file for _, access_file, _, _ in self._fda_by_struct.get((struct_name, struct_def_file), [])
            if self._is_port(access_file)
        }

    # ------------------------------------------------------------ local_type_uses

    def _build_local_type_indexes(self, path: Path) -> None:
        # Forward: (function_name, function_def_file) → set of
        #   (type_name, type_kind, type_def_file, use_kind).
        # Inverse: (type_name, type_def_file) → set of
        #   (function_name, function_def_file).
        # The inverse index is what `opaque_in` needs — it answers
        # "which functions mention this type in their body" by name
        # + def_file lookup. The forward index lets a future
        # consumer enumerate everything a particular function uses
        # locally (not consumed in v1).
        self._ltu_forward: dict[tuple[str, str], set[tuple[str, str, str, str]]] = defaultdict(set)
        self._ltu_inverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        if not path.exists():
            # local_type_uses.csv is optional — if the extraction
            # pipeline ran without it (older T2 set), reach still
            # works but opaque_in will fall back to the
            # signature-only sound subset.
            return
        for r in _load_csv(path):
            fn = (r["function_name"], r["function_def_file"])
            self._ltu_forward[fn].add(
                (r["type_name"], r["type_kind"], r["type_def_file"], r["use_kind"])
            )
            for ty in self._inverse_type_keys(
                r["type_name"], r["type_def_file"], r["type_kind"]
            ):
                self._ltu_inverse[ty].add(fn)

    def functions_using_type_in_body(
        self, type_name: str, type_def_file: str
    ) -> set[tuple[str, str]]:
        """Inverse local-type-uses lookup: functions whose body
        declares a local of this type, casts to it, or applies
        `sizeof` to it. Disjoint use-kinds are unioned — the
        consumer only cares "does the function mention T at all".

        Combined with `functions_using_type` (signatures) and
        `field_access_records` (transparent uses), this gives the
        full membership set for `opaque_in` / `non_opaque_in`
        partitioning.
        """
        return set(self._ltu_inverse.get((type_name, type_def_file), set()))

    def types_in_body_of(
        self, fn_name: str, fn_def_file: str
    ) -> list[tuple[str, str, str, str]]:
        """Forward: every (type_name, type_kind, type_def_file,
        use_kind) the given function mentions in its body via local
        var / cast / sizeof. Reserved for future consumers; not
        used by v1 manifests.
        """
        return list(self._ltu_forward.get((fn_name, fn_def_file), set()))

    # ---------------- signature-type queries

    def types_in_signature_of(self, fn_name: str, fn_def_file: str) -> list[tuple[str, str, str, str]]:
        """List of (type_name, type_kind, type_def_file, position)
        entries for the given function's signature. Empty when the
        function has no user-defined types in its signature.
        """
        return list(self._stu_forward.get((fn_name, fn_def_file), []))

    def casts_to(self, tag: str) -> list[str]:
        """Tags this type is cast INTO (the type appears as a cast operand).
        Sorted. The type's raw `casted.to`."""
        return sorted(self._cast_to.get(tag, set()))

    def casts_from(self, tag: str) -> list[str]:
        """Tags cast INTO this type (the type appears as a cast result).
        Sorted. The type's raw `casted.from`."""
        return sorted(self._cast_from.get(tag, set()))

    def callback_sig_types_of(self, cb_name: str, cb_def_file: str) -> list[tuple[str, str, str, str]]:
        """Signature type uses of a CALLBACK typedef — `(type_name, type_kind,
        type_def_file, position)`, same shape as `types_in_signature_of` but
        keyed on the function-pointer typedef. Drives `depends_on.types` on
        callback entries. Empty when the callback's signature has no
        user-defined types.
        """
        return list(self._cbstu_forward.get((cb_name, cb_def_file), []))

    def callbacks_invoked_by(self, fn_name: str, fn_def_file: str) -> set[str]:
        """Forward: the callback typedef names this function INVOKES (indirect
        call through a function-pointer value). The inverse of
        `callback_callsites_of`; drives the invoker's `depends_on.syms`.
        """
        return set(self._cb_invoked.get((fn_name, fn_def_file), set()))

    def callback_callsites_of(self, cb_name: str, cb_def_file: str) -> set[str]:
        """Function names that **invoke** this callback (indirect call through a
        value of its type) — the contract-bearing subset of its consumers,
        excluding pass-through declarations. Drives `callsites` on callback
        entries; the wrap stage reads these bodies to decide arg/return ownership.
        """
        return set(self._cb_callsites.get((cb_name, cb_def_file), set()))

    def functions_using_type(self, type_name: str, type_def_file: str) -> set[tuple[str, str]]:
        """Inverse: functions whose signature mentions this type.
        Used for type-manifest's `used_by` field and for `ops[]`
        candidate-set discovery downstream.
        """
        return set(self._stu_inverse.get((type_name, type_def_file), set()))

    # ---------------- forward edges for `depends_on` population

    def callees_of(self, fn_name: str, fn_def_file: str) -> set[tuple[str, str]]:
        """Set of (callee_name, callee_def_file) directly called by
        the given function. Drives `depends_on.syms.{port,wrap}` for
        functions; scope-split is the caller's responsibility.
        """
        return set(self._fc_forward.get((fn_name, fn_def_file), set()))

    def addr_targets_of(self, enc_name: str, enc_file: str) -> set[tuple[str, str]]:
        """Set of (target_name, target_def_file) whose address is
        taken in the given enclosing scope.
        """
        return set(self._fa_forward.get((enc_name, enc_file), set()))

    def globals_used_by(self, fn_name: str, fn_def_file: str) -> set[tuple[str, str, str]]:
        """Set of (global_name, global_def_file, access_kind) the
        given function reads / writes / takes the address of.
        """
        return set(self._ga_forward.get((fn_name, fn_def_file), set()))

    def macros_expanded_by(self, enc_name: str, enc_file: str) -> set[tuple[str, str]]:
        """Set of (macro_name, macro_def_file) the given enclosing
        scope expands. Includes both function-body and file-scope
        expansions (the latter when enc_name="" and enc_file is the
        port file containing the file-scope instantiation).
        """
        return set(self._me_forward.get((enc_name, enc_file), set()))
