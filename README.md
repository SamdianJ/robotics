# Gym-Anything for Robotics

Gym-Anything for Robotics is a framework in which coding agents build realistic robotics simulations. This
repository contains the framework and **RoboWorld**, a benchmark of 25 simulations of economically valuable
physical work, from welding and sewing to wine pouring and forklift driving. Each simulation models the physics its
job needs, such as liquids, cloth, molten metal, soil and soft tissue, and runs with several robots, from single arms
to bimanual arms, mobile manipulators and humanoids.

Each simulation ships a technical report (`REPORT.pdf` in its folder) on its physics, parameters, validation
experiments, tasks and limitations. See [`benchmarks/roboworld`](benchmarks/roboworld) for the list.

## Quickstart

RoboWorld needs Linux and an NVIDIA GPU. Camera images are rendered by Isaac Sim, which needs a GPU with RT cores
(for example L40S or RTX 4090/5090. A100 and H100 cannot render). Physics runs in Python 3.12 with Newton.

```bash
# 1. Install. Labels and printed artwork use the DejaVu fonts, and recordings use ffmpeg.
sudo apt-get install fonts-dejavu-core fonts-dejavu-extra ffmpeg
# We recommend uv for Python: https://docs.astral.sh/uv/
uv venv --python 3.12.12
source .venv/bin/activate
uv pip install -r requirements/newton.txt -e ".[all]"

# 2. Point RoboWorld at Isaac Sim's own Python, which renders the cameras (see "Rendering" below)
export ISAAC_RENDER_PYTHON=/path/to/isaac-sim/python

# 3. Check what is available on your machine
gar list
gar doctor welding

# 4. Start a task. The server prints its URL, and `gar keys` shows its two API keys.
gar tasks resolve welding --selector-json '{"task": "fillet-2f"}' --output welding.task.json
gar run --task-file welding.task.json --port 8080 --policy-key
```

The same steps work in the reference container, which pins every version used for the paper:

```bash
docker build -f docker/Dockerfile -t gym-anything-robotics .
docker run --gpus all -it -p 8080:8080 gym-anything-robotics
```

## Control a robot

Every running simulation is an HTTP server. `GET /v1/help` documents the whole API, and `GET /v1/capabilities` lists
the robot commands, cameras and proprioception of the current setup. A session has two keys:

- The **policy key** gives what a real robot has: robot commands, camera images and proprioception, and the task
  description (`GET /v1/task`).
- The **owner key** also reads the simulator's privileged state (`/v1/state`, `/v1/geometry`, `/v1/history`),
  saves and restores checkpoints, resets the episode, and reads the task evaluation (`GET /v1/evaluation`).

```bash
gar keys welding                 # both keys of the running session (secret output)
gar observe welding              # the policy observation
gar verify welding               # the task evaluation: success and a reward in [0, 1]
gar stop welding
```

From Python:

```python
import gym_anything_robotics as gar

task = gar.resolve_task("welding", {"task": "fillet-2f"})
env = task.create()          # runs the simulation in this process
obs = env.obs()
result = env.evaluate()      # {"success": ..., "score": ...}
env.close()
```

## Evaluate a coding agent

[`agents/`](agents) runs Claude Code or Codex on RoboWorld tasks. The agent gets the task description, the simulation's
URL and the policy key, and a time budget, and it works in an isolated container that holds nothing but its own files.

```bash
python -m agents.evaluate --split test --agent claude-code --model claude-opus-5-5 --effort xhigh
python -m agents.evaluate --split test --agent codex --model gpt-6-astra --effort xhigh
```

`--split train` runs the train split instead.

[`agents/learning.py`](agents/learning.py) runs the learning settings of the paper: in-context learning across
repeated attempts, a privileged teacher, a coached student that receives qualitative hints from the teacher, and
self-transfer, in which the teacher continues without privileged access.

## Build a simulation

The creation-audit loop is part of the framework. A creation agent researches and builds a simulation, then gets one blind
nudge to keep improving it. Four audit agents (qualitative, evidence, infrastructure and hack) then check it and
report issues for the creation agent to fix. The agents run on Claude Code (the default) or Codex. See [`src/gym_anything_robotics/creation_audit`](src/gym_anything_robotics/creation_audit).

```bash
gar create-audit --action-group PATH_TO_ACTION_GROUP --env-dir benchmarks/roboworld/environments/NEW_ENV
# Or run creation and auditing separately:
gar create --action-group PATH_TO_ACTION_GROUP --env-dir benchmarks/roboworld/environments/NEW_ENV
gar audit --env-dir benchmarks/roboworld/environments/NEW_ENV
```

`create-audit` runs creation, an initial four-role audit, then three repair/re-audit cycles by default.
Every repair resumes the original Creator session with all four preceding reports; every re-audit uses fresh
auditor sessions. Thus the final repaired environment is audited too (four audit batches in total).
`--rounds` changes the number of repair/re-audit cycles. Completion means all scheduled reports were generated,
not that the environment passed physical qualification. The bundled
[environment guide](src/gym_anything_robotics/creation_audit/memory/environment_guide.md) supplies quality criteria
and references to the integration code shipped in this checkout.

## Rendering

Isaac Sim renders the cameras in its own Python interpreter, separate from the Newton physics runtime. Install Isaac
Sim 5.1 (`pip install 'isaacsim[all,extscache]==5.1.0.0' --extra-index-url https://pypi.nvidia.com` in a Python 3.11
environment, see [`docker/Dockerfile`](docker/Dockerfile)) and set `ISAAC_RENDER_PYTHON` to that environment's
`python`. Laundry, football and titration also read `LAUNDRY_RENDER_PYTHON`, `FOOTBALL_RENDER_PYTHON` and
`TITRATE_RENDER_PYTHON`. Without a renderer, sessions can still run without cameras.

## Tests

```bash
uv pip install -e ".[dev]"
python -m pytest tests
```

The default run is CPU only and needs no robot asset downloads. Tests that need a GPU, Isaac Sim or long physics
runs are skipped unless their variable is set, for example `GAR_GPU_TESTS=1` or `GAR_WELDING_PHYSICS_TESTS=1`.
Those tests were not run for this release.

## Repository layout

```
src/gym_anything_robotics/     core: environment API, HTTP server, sessions, the gar CLI, the creation-audit loop
src/robots/                    shared robot catalog: models, rigs and controllers
benchmarks/roboworld/          the RoboWorld benchmark
    environments/<sim>/        one package per simulation (physics, scene, tasks, verifiers, renderer, REPORT.pdf)
    splits/                    task splits
agents/                        coding-agent evaluation and learning in simulation
tests/                         unit and contract tests (CPU)
docker/                        reference runtime image
requirements/                  exact versions of the reference runtime
```

## License

This repository is released under the MIT License. Third-party robot models, meshes and textures keep their own
licenses, recorded next to the assets that use them.
