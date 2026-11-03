# Environment creation guide

This guide describes the quality and integration requirements used by this
repository's creation and audit prompts. It consolidates the shipped prompts;
it is not a reconstruction of the original unpublished research notes or a
claim that missing historical documents were reviewed.

## Quality and evidence

- Model the physical mechanisms needed for meaningful tasks. Explain equations,
  units, numerical choices, measured quantities and approximations. Ground
  parameters in available sources and distinguish estimates from measurements.
- Build a plausible setting for the actual activity, with appropriate equipment,
  layout, materials and lighting. Inspect renders and motion, not only exports.
- Demonstrate stationary stability, important dynamic interactions and assisted
  solvability. Keep videos, supporting scripts and reproducible commands. Label
  phantom forces and other assistance separately from normal policy actions.
- Exercise legal actions to look for NaNs, infinities and implausible motion.
  Capture starting conditions and reproductions of failures. A failed robot task
  is not necessarily a simulator defect.
- Measure wall-clock time per simulated second against the 200x target, stating
  hardware, camera configuration, cold/warm startup and concurrent GPU load.
- Deliver a technical report with sources, models, experiments, evidence and
  limitations. Refresh evidence after relevant implementation changes.

## Existing integration code

The following paths are relative to the shared repository checkout. Read the
actual implementation and tests for the current contracts:

- `src/gym_anything_robotics/env.py`: environment and observation lifecycle.
- `src/gym_anything_robotics/registry.py`: backend registration and discovery.
- `src/gym_anything_robotics/launch.py`: launch configuration and diagnostics.
- `src/gym_anything_robotics/tasks.py`: task catalogs and resolution.
- `src/gym_anything_robotics/http_server.py`: HTTP access and action handling.
- `src/gym_anything_robotics/cli_parser.py`: CLI commands and options.
- `src/gym_anything_robotics/session_service.py`: session and dashboard serving.
- `src/robots/catalog.py`, `src/robots/spec.py`, `src/robots/runtime.py`:
  reusable robot models, configurations and runtime interfaces.
- `tests/test_env.py`, `tests/test_tasks.py`, `tests/test_http_server.py`:
  executable contract examples; passing them alone is not physical validation.

Use the existing CLI, HTTP and dashboard contracts for instructions, controls,
observations and outcome evaluation. Declare supported robot configurations and
reuse shared robot assets and templates.

## Reference environments and retained lessons

Inspect relevant code and `REPORT.pdf` files under
`benchmarks/roboworld/environments/whiteboard/`,
`benchmarks/roboworld/environments/titrate/` and
`benchmarks/roboworld/environments/deposit_cans/`. These are starting points, not
proof that a new environment meets the requirements. Evidence may require
regeneration or additional asset downloads; disclose unavailable evidence.

Read any project-specific lessons actually present in the checkout. Record new
reusable lessons in the Creator handoff with supporting evidence. Do not assume
that historical `docs/lessons/` or unpublished quality/contract documents exist.
The four audit prompts alongside this guide define the independent review roles.
Auditors and Creators share a writable checkout; separate agent sessions do not
provide filesystem isolation. Keep audit records intact, preserve unrelated
services, and clean up only workers created for your own experiments.
