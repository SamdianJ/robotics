# Fixed interface and evidence checks

Use `gar check --plan PATH --output NEW_DIRECTORY` for repeatable base checks.
It starts a new loopback session on an allocated port with its own private keys
and session directory, executes bounded actions, and stops that session. It
does not attach to a user's existing simulation and does not call a model.

Create `checks.json` in the environment directory during the build. The audit
launcher runs it once before each batch of auditors, retains its `results.json`,
and supplies that path to all four auditors. The outer repair pipeline includes
the exact results and their SHA-256 in the next feedback packet. A failed fixed
check is evidence for repair; the independent auditors still run. An environment
without a plan is explicitly `not_configured`, never implicitly passed.
`gar audit` and `gar create-audit` accept `--check-plan PATH` as an override.

## Plan format

Paths are relative to the plan file. Use a launch configuration OR a `task_file`
containing a resolved `gar.task.v1` task; never combine both. Session artifacts
are owned by the checker, so do not specify a backend `output_dir`.

```json
{
  "schema": "gar.check-plan.v1",
  "launch": {
    "schema": "gar.launch.v1",
    "environment_id": "tabletop-push",
    "backend_options": {}
  },
  "cases": [
    {"name": "hold-1ms", "action": {"type": "named_joint_position", "targets_rad": {}, "ticks": 1}, "sim_s": 0.001, "repeat": 3},
    {"name": "hold-10ms", "action": {"type": "named_joint_position", "targets_rad": {}, "ticks": 10}, "sim_s": 0.01, "repeat": 3},
    {"name": "hold-100ms", "action": {"type": "named_joint_position", "targets_rad": {}, "ticks": 100}, "sim_s": 0.1, "repeat": 3}
  ],
  "max_wall_per_sim": 200,
  "startup_timeout_s": 180,
  "request_timeout_s": 120,
  "images": true,
  "checkpoint": true,
  "checkpoint_ignore_fields": {"generation": "Monotonic branch identifier incremented by this backend on checkpoint restore; not physical state"},
  "physics_trace": true,
  "source_files": ["env.py", "controller.py", "config.py"],
  "videos": [{"path": "evidence/push.mp4"}]
}
```

The example requires the generated tabletop-push environment; it is not an
environment bundled in the upstream checkout. Use the actual action contract
and expected simulated duration for your environment. The first case must be
a small legal hold: it is also used for warm-up, idempotency and checkpoint
continuation. Cases execute in listed order in the same session after those
checks, without resets. Each has 1..100 repetitions; at most 32 cases are allowed.
Add a known failure's exact public action sequence as a named regression case.
The tabletop joint-4 endpoint regression is 14 requests of 500 ticks to
`arm/fr3_joint4: -0.1518`, at a 1 ms timestep (7 simulated seconds). The original
failure occurred near step 6201. This checks the known endpoint fault, not all
joint limits, collision fidelity or task solvability.
The corresponding versioned regression recipe is retained in the repository at
`tests/fixtures/checks/tabletop-push.json`. Run it only in a checkout where that
generated environment is registered; it intentionally does not bundle the large
experiment or install/register a new environment automatically.

Each video may specify `frames` (positive exact decoded frame count) and `sha256`.
The tool fully decodes the video with ffmpeg and records actual frame count/hash.
Missing ffmpeg, missing/corrupt media or mismatches fail that configured check.
An empty video list is reported as skipped. Decoding does not establish visual
quality, frame-to-physics correspondence or absence of fabricated motion; those
remain the evidence auditor's responsibilities.

## Checks and interpretation

- Real CLI discovery, doctor and registered task catalogues in text, verbose
  and JSON modes; managed launch and shutdown.
- Policy camera download and full image decoding against advertised dimensions;
  observation/evaluation reads must not change state.
- Policy access to privileged state, evaluation, history, geometry and session
  endpoints must be denied. Unknown actions must reject without state mutation.
- Idempotent retry executes once; pause rejects motion; checkpoint restore and
  repeated continuation compare the full returned state except explicitly declared
  top-level `checkpoint_ignore_fields`. Each exclusion needs a nonempty reason,
  must exist in the state, and cannot remove all state fields. Use this only for
  documented bookkeeping such as a monotonically increasing branch identifier;
  preserve physical positions, velocities, controller and evaluator state. Raw
  before/after states and the exclusions are retained in `checkpoint.json`.
- Every configured action must commit with an observation, advance the declared
  simulated time and leave finite JSON state. Timing includes the HTTP action,
  fresh observation, all returned images' download and decoding. Initial launch
  and the separate state reads are excluded and disclosed separately. The
  default wall/simulated-time limit is 200; do not change it to hide a failure.
- History must retain all successfully checked requests with no recording fault
  or eviction. Each retained action's trace header must report complete capture,
  no missing steps and no capture errors. Checkpoint rewinds are legitimate;
  completeness is evaluated per action rather than assuming global monotonicity.
- `gar verify` exit 1 with a valid available/unsuccessful evaluation is expected.
  Exit 2 is valid only for an unavailable evaluator with the matching payload.
  An arbitrary error with those exit codes is not accepted.

Images, checkpoints and physics traces default to required. An explicit `false`
for a capability is a coverage exclusion, not proof of that capability. Camera
images that are returned are still decoded even when an imageless plan is allowed.
Checks are bounded by startup/request/media timeouts and finite case counts;
they do not retry an ambiguously executed motion. SIGINT/SIGTERM retain partial
results and attempt cleanup. Unrecoverable process/host termination may still
require operator cleanup; this tool is not a pipeline checkpoint-resume system.

`results.json` has a versioned schema, per-check `passed`/`failed`/`skipped`
statuses, measurements, plan/tool/selected-source hashes and artifact paths.
CLI captures, decoded image files and checkpoint/history records are saved
beside it. The `private/` directory contains session logs and mode-0600 keys;
do not publish that directory. Public JSON outputs redact known bearer tokens.
Selected source hashes are not a claim to fingerprint every dependency.

Exit 0 means all configured fixed checks passed, 1 means failure, and 130 means
interruption. None of these outcomes establishes physical qualification.
Preserve the original results. Add scene-specific experiments only for coverage
these tools do not provide; do not replace or weaken the fixed probe to turn a
failure into a pass.
