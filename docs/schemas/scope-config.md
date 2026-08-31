# scope-config.json schema

Field meaning for
`<repo_root>/crustify/campaigns/<target>/<sub-campaign>/scope-config.json`.
Layout example: [`specs/scope-config.json`](../../specs/scope-config.json).

The orchestrator authors one config per sub-campaign before asking the oracle
for a schedule. It is the durable record of the selection submitted to the
campaign-wide oracle target and the exact closure the oracle returned.

| field | meaning |
|---|---|
| `name` | sub-campaign identifier; normally the directory name |
| `objective` | `port`, `wrap`, or `raw-lifetime` |
| `oracle_target` | campaign-wide oracle target used to resolve the selection |
| `selection` | user-selected subsystem identities and any narrower type/symbol selection |
| `closure` | exact oracle-resolved `targeted` and `imported` sets |

## selection

`subsystems` is a list of `(link_unit, name)` identities from
`subsystems.json`. `types` and `symbols` narrow the selected subsystem when the
user requested named entities; otherwise they are empty. Named selection
entries use the same `name`/`defined_in`/`declared_in` identity records as the
closure. `lifetime_for` is
`"void"` or `"string"` only for a raw-lifetime sub-campaign and `null`
otherwise.

## closure

Both `targeted` and `imported` contain exact, sorted lists of
`implementation_files`, `types`, and `symbols` returned by the oracle. Type and
symbol entries are records containing `name`, `defined_in`, and `declared_in`;
do not reduce them to bare names because file-local names can repeat across
translation units. `defined_in` may be `null`, and `declared_in` is always a
list. Do not estimate these sets or copy a directory-level approximation into
them. Update the config after resolving the closure and before producing its
first wave.

The closure may overlap an earlier producer sub-campaign. Scheduling excludes
items already completed by those bottom-up predecessors; the config retains the
full resolved closure so the dependency relationship remains explicit.

Raw `void` and raw `string` lifetime discovery are separate sub-campaigns and
therefore have separate configs. Their subsystem, type, and symbol selections
are empty; `lifetime_for` distinguishes them, and the oracle populates their
exact closure sets.
