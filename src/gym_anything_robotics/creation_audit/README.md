# Creating and auditing simulations

This folder holds the creation-audit loop used to build the RoboWorld simulations. A creation agent builds a
simulation, and four audit agents check it and write reports that the creation agent then fixes. The auditor launcher
follows the auditor half of [Gym-Anything's creation_audit/method.py](https://github.com/cmu-l3/gym-anything/blob/1a2b4d94114da9733b6544d3275728c2a2ae84a0/extras/research/software_as_env/creation_audit/method.py)
(MIT, see [UPSTREAM_LICENSE.txt](UPSTREAM_LICENSE.txt)).

Both commands run a coding-agent CLI with full access to the workspace, so run them on a machine you are willing to give
the agent. Claude Code is the default backend (`--backend cc`, model `claude-opus-5-5`, effort `xhigh`). Codex is the
other backend (`--backend codex`), and it uses the model in your Codex config. `--model` overrides the model.

## Run the complete loop

```bash
gar create-audit --action-group PATH_TO_ACTION_GROUP --env-dir benchmarks/roboworld/environments/NEW_ENV
# Use the configured Codex model instead of the default Claude backend:
gar create-audit --action-group PATH_TO_ACTION_GROUP --env-dir benchmarks/roboworld/environments/NEW_ENV --backend codex
```

The scheduler first runs Research, Build and Blind nudge in one Creator session,
then runs an initial audit with all four roles. It performs **three repair/re-audit
cycles** by default (`--rounds 3`):

1. Collect all four preceding reports into a feedback file, retaining their paths
   and SHA-256 digests. The packet contains the full reports, not a model summary.
2. Resume the original Creator session with the [repair prompt](memory/env_creation_notes/repair_prompt.md).
   The Creator investigates findings, fixes causes, refreshes affected evidence,
   and writes a new handoff describing fixes, verification and unresolved issues.
3. Run all four auditors again, each in a fresh session. These reports become the
   next cycle's feedback; the last cycle's reports review the final repaired code.

Three cycles mean three repairs and four full audit batches, including the initial
audit. Each auditor makes two CLI calls (explore, then audit), so the default run
has 38 calls: 3 creation calls + 8 initial audit calls + 3 × (1 repair + 8 audit calls).
Auditors run sequentially in the shared checkout. Independent sessions do not
isolate the filesystem or enforce read-only access.

The pipeline fails if creation fails, a required report is missing/empty, a repair
fails or changes session identity, or any audit batch fails. A failed audit batch
is not used as complete feedback. No later repair cycle starts after a failure.
Completed records remain available; interruption is recorded in the run manifest.
`--timeout-sec` applies to each agent invocation, not to the whole pipeline.
The existing invocation runner cleans up its own agent subprocess group on timeout
or interruption. This command does not automatically resume an interrupted run;
inspect its retained records before deciding how to continue. Existing output
directories are never overwritten.

Artifacts are saved under `artifacts/pipelines/<environment>-<time>-<id>/`:

```text
run.json                    overall status, stage, Creator ID and cycle records
creation/                   research, build, nudge and their logs
initial-audit/              first four reports, prompts and logs
round-01/                   repeated for round-02 and round-03
  feedback.md               complete preceding reports and source digests
  repair-prompt.md           exact request given to the Creator
  repair.jsonl              Creator response log (with separate stderr log)
  repair-handoff.md          fresh repair record
  audit/                    independent re-audit reports, prompts and logs
```

**Exit code zero means the scheduled workflow completed, not that the simulation
passed.** Negative findings do not cause early exit or get reclassified as passing.
Read the final reports for the remaining issues and qualification evidence.

Default research, build and repair prompts reference the bundled
[environment guide](memory/environment_guide.md), using its resolved filesystem
path. It lists existing source contracts, tests and reference environments. This
replaces references to unpublished `docs/` material; no missing historical notes
or original user wording have been fabricated. Record new lessons in the handoff.
`--prompt-dir` overrides all four Creator prompt files (including `repair_prompt.md`);
`--memory-dir` overrides the four audit prompts for this pipeline.

## Create a simulation

```bash
gar create --action-group PATH_TO_ACTION_GROUP --env-dir benchmarks/roboworld/environments/NEW_ENV
```

The action group is a file or folder that describes the job: its actions and the capabilities they need. The creator
runs three turns in one agent session:

1. [Research](memory/env_creation_notes/research_prompt.md): the agent studies the job and designs the simulation and
   writes `research.md`.
2. [Build](memory/env_creation_notes/build_prompt.md): sent only after the research succeeds, with the research still in
   context.
3. [Blind nudge](memory/env_creation_notes/nudge_prompt.md): a short request to keep improving the simulation, sent
   after the build succeeds.

Prompts, logs and the session ID of each turn go to `artifacts/creations/<environment>-<time>-<id>/`.

## Audit a simulation

```bash
gar audit --env-dir benchmarks/roboworld/environments/NEW_ENV
gar audit --env-dir benchmarks/roboworld/environments/NEW_ENV --auditor evidence
```

All four auditors run by default. Repeat `--auditor` to pick some of them.

- [Qualitative](memory/qualitative_prompt.md): task-relevant physics, scene realism, relevance to the real job and
  grounded parameters.
- [Evidence](memory/evidence_prompt.md): demonstrations of the dynamics and of each interaction.
- [Infrastructure](memory/infrastructure_prompt.md): speed, completeness of the API and CLI, and reuse of the shared
  robot templates.
- [Hack](memory/hack_prompt.md): tries legal actions that break the simulation, and records how to reproduce them.

Each auditor gets a fresh session, explores the repository, then reads its prompt in the same session and writes
`audit.md`. Reports, prompts and logs go to `artifacts/audits/<environment>-<time>-<id>/`. Exit code zero means every
report was written, not that the simulation passed. Each call times out after two hours by default (`--timeout-sec`).

The evidence auditor can also send videos to Gemini (`gemini-3.8-flash`) with a [video tool](memory/video_tool.md). It
needs `pip install -e '.[creation-audit]'` and `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Without them the auditor is told
that the tool is not available.
