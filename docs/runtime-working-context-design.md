# Runtime Working Context

## Goal and boundaries

Compile each model request from the current work state, rather than send the
entire growing tool transcript. Retain one analytical owner and one final Python
program. Preserve preparation, mixed sources, clarification, external output
formats, no-data handling, local rerun and post-analysis Evidence.

The graph transcript is the lossless audit and resume record. The model request
is a disposable projection. Never mutate persisted messages to reduce context.
An older failure is superseded by a new submission, not proven resolved.

## Work states

| State | Entry | Mandatory material | Execution tool |
| --- | --- | --- | --- |
| understand | initial request | task, rules, preparation, schema workspace, existing answers, capability directory | none |
| probe | explicit work selection | above plus Python execution and all selected-source query contracts, existing observations | probe |
| implement | explicit work selection | above plus complete output contract | formal program |
| repair | failed formal execution | current program, latest failure, unresolved history, implementation contracts | formal program |
| waiting | clarification interrupt | persist transcript; no model request until answer | existing clarification flow |
| complete | saved result | existing termination middleware; Evidence remains separate | none |

The model selects business focus and proposes probe/implementation work. Local
execution outcomes determine failure, wait and completion. A successful probe
does not prove readiness for implementation. No semantic report blueprint or
new independent model stage is introduced.

## Dependency policy

- Required contracts come from work state, selected-source capability union,
  output format and clarification mode, not solely from model retrieval.
- A complete-code repair retains all implementation dependencies, not merely
  those of the failing line. No source is dropped to satisfy a token budget.
- Reference reads request additional material. Contract IDs are de-duplicated;
  explicit references remain pinned in this first version. Unloading an explicit
  reference without a proven dependency lifetime is unsafe.
- Initial understanding retains schema/preparation facts conservatively. Further
  schema projection requires independent completeness tests; this change must
  not invent field-level relevance from arbitrary text.
- Exact module content is rendered from the existing registry. A reference's
  examples remain available; its core definitions appear only once per request.

## Memory projection

### Typed memory is the management unit

Messages are transport/audit records, not memory identities. Each managed item
needs: kind, stable logical key, revision/content hash, source event, applicability
(task/source/query/code scope), dependencies, status and replacement lineage.
Store original content once; do not ask the model to re-emit large source facts.

| Kind | Identity and update policy | Delivery |
| --- | --- | --- |
| user requirement / clarification | authoritative message/item ID; explicit user revision only | relevant work throughout |
| preparation fact | source/stage plus fact or relationship ID; advisory status preserved | understanding and affected work |
| schema definition | datasource + field/path + snapshot version; union distinct requested fields | work dependencies |
| protocol | contract ID + version + output mode; exact duplicate rendered once | mandatory dependency closure |
| probe observation | execution + query + observed property + query scope; no last-write-wins across populations | unresolved/current query dependencies |
| analytical decision | explicit model declaration with assumptions and scope; never inferred from hidden reasoning | current and dependent work |
| program | program revision; patch depends on base revision; new full version supersedes old source | current implementation/repair |
| execution failure | execution + error identity; new execution does not prove resolution | current diagnosis; earlier unresolved summary |
| artifact result | execution + artifact ID; preserved independently of model view | post-analysis / explicit followup |
| unknown payload | source event identity; do not infer merge/replacement semantics | conservative retention |

Kinds define legal operations, not merely labels. Identical content may still
describe different populations. Different content may be revisions of the same
program. Deduplication, supersession, invalidation and unloading are separate
operations. Unloading never deletes the original fact; unsupported input stays
available instead of being silently classified or discarded.

Adapters obtain type/identity from existing producer APIs (user messages,
Preparation contracts, Schema paths, query outcomes and reference registry).
Only model-owned work focus and analytical decisions require model input.
Do not add a model classification call per memory item. The first request-view
implementation below is an incremental adapter, not yet the full typed-store
implementation. Its exact-snapshot and code replacement rules must migrate to
this identity model before introducing broader semantic pruning.

Current implementation status:

- The request compiler builds a typed index from the lossless message
  transcript and the current Preparation/Schema/contract context. It already
  drives full-program supersession, structured-Probe source retirement and
  per-request audit logging.
- Existing `short_term_memory` remains the compatibility store for
  clarification restore, exact Schema reads and Probe fact ledgers. Those
  values must be migrated one producer at a time to typed adapters; do not
  replace it wholesale or duplicate it as a second mutable source of truth.
- `context_mode` is a runtime-owned state item and must be carried to contract
  selection on every projected request. A follow-up must never silently load
  the fresh-analysis contract set.

- Preserve user instructions, clarification answers, assistant analytical prose,
  unknown tool payloads and actual probe observations.
- Keep the latest full formal program; older full versions are represented by
  call IDs. Keep patch chains intact until a subsequent full program supersedes
  them. Do not pretend a patch is a complete program.
- Keep current error context. Remove duplicate copies of code and old stack/
  source-location details from older errors, retaining their error messages and
  semantic diagnostics. Older failures are not automatically marked resolved.
- Remove repeated formal contract packets from probe feedback, because the
  request compiler supplies the state-required contract once.
- Preserve tool-call/result pairing and original call IDs in the projected
  history. Projection must be deterministic and must not mutate source history.
- Log raw/view sizes, state, dependency IDs and archived-code count without
  credentials or message bodies.

## Implementation and acceptance

1. Add pure state/dependency/projection functions and their tests.
2. Add local work-selection tool and request middleware (sync and async).
3. Align resident prompt instructions with actual tools and state transitions.
4. Run preservation tests: full code repair, patch chain, multiple probes,
   repeated reference, custom diagnostics, mixed sources, JSON/text outputs,
   clarification transcript restore, no mutation and valid tool pairing.
5. Replay captured requests offline. Check both missing information and repeated
   material; do not optimize only token counts.
6. Only then run real regression sequentially. Compare report correctness,
   artifact/Evidence coverage, calls, per-call context and total elapsed time.

No real regression pass is claimed by implementing this design. Remaining
analytical prose and observations can still grow; the first version prioritizes
removing provably redundant material over lossy semantic summarization.

## Regression findings: 2026-09-08

- Probe keeps its baseline responsibility: validate actual query scope,
  formats, enumerations, relationship reachability and data availability.
  Multiple bounded queries can belong to one Probe. Its observations do not
  replace formal calculations or determine report structure.
- Work-view size is an observation, not a hard acceptance limit. Preserve
  necessary facts and protocols; remove only demonstrably obsolete or duplicate
  material. Normal size variation is acceptable.
- Conversation 43 / turn 63 exposed Probe source submitted through the formal
  tool. Rejecting it alone was incomplete: the next work view still selected
  formal repair. The explicit `formal_tool_received_probe_program` diagnostic
  now selects Probe repair protocols and the matching tool. General formal
  failures still select formal repair.
- The same run later reached complete formal source generation but failed
  query-constraint schema validation twice. Exact object types were documented
  in the old execution.core summary but incompletely inherited by the active
  execution.implementation pack. That pack now includes the object definition
  and a literal example verified against the actual runtime schema validator.
- These are locally verified fixes, not a real regression pass. The stopped
  run and its artifacts remain under
  `.codex-regression/goal-runtime-20260906/case-43-63-probe-purpose-current`.
  New real execution, report quality and Evidence acceptance remain pending.
- Code argument aliases accepted by execution tools must have the same memory
  lifecycle as `code`. The memory index now recognizes all accepted formal
  source spellings, and the outbound copy normalizes them before supersession.
  Source precedence matches the tool; original audit messages remain intact.
- Offline replay verified latest-source retention, immutable audit history and
  equivalent restoration on the final three captured requests. The current
  replay harness does not reconstruct the complete datasource capability
  snapshot, so its token counts are not a performance comparison and its
  output does not establish real query-contract completeness.
- Keeping facts alone is insufficient for action continuity. The most recent
  successful Probe call/result pair must remain visible until a newer execution
  supersedes it. Otherwise the last visible work-selection event can still say
  Probe even when the active tool surface has moved to implementation. Older
  covered Probe pairs can be removed without removing their typed facts.
- Real run 43:63, task 537, execution 2131 completed in 637.687 seconds with one
  Probe and two formal repairs. This supports the action-continuity fix, but
  does not pass acceptance. Preparation completed on its first attempts.
  Formal failures were excessive grouped-query output followed by comparison
  of a string-valued derived aggregate with an integer.
- Content review failed: ratios below one were described as strong positive
  association; batch dispersion was called continuous drift without temporal
  continuity evidence. Both were present in the first formal program. The
  first repair additionally replaced a missing comparison group with zero.
  These require separate analysis of initial reasoning and repair preservation;
  neither can be solved by treating successful execution as semantic validity.
- Evidence compiler reported complete (49 definitions, 7 chart references,
  16 table references, 135 report references). This is a compiler status only,
  not proof of correct analytical claims or independently verified coverage.
  Saved artifacts: `case-43-63-1788852013` under the regression directory above.
