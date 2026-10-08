# Runtime checkpoint and cross-source regression (2026-10-08)

This checkpoint preserves the executable analysis / result expression separation,
runtime-owned native Evidence, asynchronous legacy Evidence, query receipt ownership,
missing-value persistence, and report/chart presentation changes. It is an engineering
baseline for cross-source regression, not a declaration of analytical acceptance.

Device v14 was imported with explicit user authorization into remote conversation 45 /
original turn 65 as execution 4183. Normal detail/history APIs return the new report,
10 charts and 7 tables. All 62 artifact references resolve as available. Old results
remain archived; no turn was added. This was result import, not service deployment.

Pre-commit verification:

- Frontend Markdown renderer: 3 tests pass.
- Previously recorded focused runtime selection: 627 tests pass.
- Full `pytest tests -q`: seven failures remain. Database-dependent namespace seeding
  cannot connect to configured remote port 9000; one legacy serialization assertion
  rejects the newly added empty `runtime_evidence` field; four scripted graph tests
  submit the old `code` payload to the native definition port and eventually encounter
  `stop_after_saved_analysis.before_model` routing errors; combined-capability prompt
  is 36,767 characters against its existing 36,000-character test limit.
- No test limit was raised, no semantic validation/retry was added, and no analytical
  capability was removed to obtain a pass.

Next regression includes non-file UNS sources. Type coverage must be confirmed from
live UNS metadata; a conversation title alone does not establish source type. Runs
use current local code and isolated result storage, with real remote read queries.
Each case retains the full SSE trace, stage/model timings, artifacts and Evidence.
