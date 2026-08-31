# Wave document schema

A campaign is the orchestrator's end-to-end translation session. Each scheduler
selection within that campaign is a **wave**. `crustify-oracle schedule` writes
one objective-neutral wave document and `crustify translate` executes it.

The filename is not identity. `schedule --output` accepts any path whose parent
directory already exists, so tracked plans normally use descriptive names such
as `crustify/campaigns/<target>/<sub-campaign>/types-l0.json`, beside that
sub-campaign's `scope-config.json`. The orchestrator scaffolds the sub-campaign
directory; the oracle never creates it. The CLI still takes the wave path
explicitly. Regardless of where the wave file lives, execution logs go to
`crustify/campaigns/<target>/logs/<session>/` using the CLI target argument and
record the submitted path.

## Version 2

```json
{
  "schema_version": 2,
  "oracle_target": "src",
  "api_headers_only": false,
  "budgets": {
    "max_syms": 50,
    "max_loc": 1000,
    "max_types": 5,
    "min_fields": 20
  },
  "summary": {
    "unit_count": 3,
    "layer_count": 2,
    "batch_count": 2,
    "file_count": 2
  },
  "plan_items": [],
  "dependency_nodes": [],
  "steps": [
    {
      "layers": [0],
      "unit_count": 2,
      "batches": [
        {
          "kind": "type",
          "source_file": "include/example.h",
          "items": []
        }
      ]
    }
  ]
}
```

`oracle_target` is the repository-relative target passed as the CLI's second
positional, or `.` for the repository root. The CLI rejects a wave submitted
against a different target. `api_headers_only` records the selection mode and
`budgets` records the packing limits used by the oracle.

`summary` gives totals for the selected units, underlying dependency layers,
emitted batches, and distinct batch source files. `plan_items` is the complete
selected workset used to render the plan. `dependency_nodes` contains referenced
nodes outside that selected set; each adds `in_scope` so the dry-run can explain
whether the dependency belongs to the target surface.

## Steps and batches

Steps execute in array order behind full barriers. A step contains one or more
topological DAG `layers`; lower steps must land before the next starts. The
oracle normally emits one layer per step. It may coalesce adjacent layers of a
closed transitive selection when the merged work still fits one batch.

The batches within one step may execute concurrently. `kind` selects the agent
route (`type`, `symbol`, or the special `raw-lifetime` route), `source_file`
records its source grouping, and `items` is the exact worklist for that agent.

Each item has this shape:

```json
{
  "name": "example_new",
  "defined_in": "src/example.c",
  "kind": "symbol",
  "source_kind": "function",
  "layer": 1,
  "loc": 12,
  "deps": {"types": [], "symbols": []},
  "fallback": [],
  "back_fill": [],
  "generates": [],
  "field_anchors": []
}
```

Every reference in `deps`, `fallback`, and `back_fill` is
`{"name": "...", "defined_in": "..."}`. `field_anchors` occurs on batched
items and lists the field accessors assigned to that type batch.
