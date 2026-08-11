# Structured Evidence Ref Evolution

## Regression facts

The isolated regressions of conversations 43, 46, and 47 confirm that
post-analysis Evidence is correctly separated from Preparation and the formal
analysis Runtime. Two executions compiled as `complete`; an empty table in
conversation 47 correctly has no numeric location to bind.

Conversation 46 exposes a real compiler-contract gap. Its root-cause ranking
table has one numeric `evidence_count` column, but each candidate row is
computed by a different branch: process, device, material, laboratory, or
packaging. The current ref shape permits only `row_scope=all`, so a table column
can carry only one Evidence calculation boundary. The compiler correctly
refuses to merge these unrelated branches, but has no representation for the
valid per-business-group boundaries and returns `partial`.

## Canonical extension

Keep Evidence at calculation-boundary granularity. Do not introduce one
Evidence per table cell. Extend `table_trace_refs` with an optional stable
`row_selector`:

```json
{
  "table_index": 1,
  "column_key": "evidence_count",
  "row_selector": {"candidate": "device_state"},
  "evidence_key": "calc_..."
}
```

`row_scope=all` remains the default for homogeneous columns. A `row_selector`
is valid only when the compiler can prove both:

1. the selector fields and values exist in the final table rows; and
2. the selected group has one independent query-to-operation calculation
   boundary.

The selector identifies a business group, not an individual cell. If no stable
grouping dimension exists, the output remains unresolved and compilation stays
`partial`; no guessed binding is allowed.

## Implemented contract

The deterministic record-list path now expands a heterogeneous table column
into selector-scoped output nodes only when every final row can be matched by a
stable literal business dimension. Each scoped node retains its own local
query-to-calculation lineage. The compiler emits the corresponding
`row_scope=selector` refs and includes the canonical selector in de-duplication.

The table renderer prefers a matching selector ref for the clicked row and
falls back to the legacy all-column ref. Existing homogeneous tables remain
unchanged. A focused regression covers independent Process and Device count
branches in one `evidence_count` column.

## Remaining implementation sequence

1. Extend the same selector-scoped path to other deterministic record builders
   where a stable business dimension and independent numeric expression can be
   proven without interpreting report prose.
2. Keep the current Structured Evidence Resolver only for branch outputs that
   local AST/dataflow cannot resolve. Its contract must classify every supplied
   output as metric, non-metric, or unresolved without replacing existing
   locally-proven output boundaries.
3. Add coverage for empty tables and genuinely unpartitionable heterogeneous
   columns; those must remain unresolved rather than receive a guessed ref.

## Non-goals

- No change to Preparation, Business Context, Probe, or formal analysis code.
- No report text parsing used to infer structured table calculations.
- No coercion of unrelated calculations into a shared Evidence definition.
- No requirement that empty tables expose a clickable Evidence location.
