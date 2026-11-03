# Build and demonstrate the robot environment

Good, now build the environment in {env_dir} from your research and design in
{research_file} for the action group at {action_group}. Your job is to finish
the environment, not to solve its tasks. Read the quality and integration
criteria in {context_file} and use the seed
environments as the minimum standard for the finished package.

Implementation, physical experiments, visual inspection, repairs and the report
go together. Do not stop at a first build milestone. Use the shared runtime and
robot catalog, with a default robot for this environment and supported robot
configuration. For an existing environment, preserve working behavior and
evidence while improving it. Follow the existing contracts linked in the guide.

Make the scene believable: objects, placement, materials, lighting and visible
behavior should belong to the activity. Implement the physical interactions
that matter, including how components work together. Look at the actual renders
and videos as you build; successful exports and unit tests do not tell you
whether the scene or motion is convincing.

Show that the environment supports its interactions through assisted evidence.
Assisted really means assisted: phantom forces and separate component
demonstrations are fine. For a whiteboard, show a hand holding a marker, cap
removal, the marker making marks against the board, and an eraser removing them.
You do not need to write a mathematical proof. Label the assistance and keep it
distinct from what a policy can do through the normal action interface.

Provide videos with their supporting scripts. Check stationary behavior over
time, important dynamic interactions and stability under legal actions. Small
settling motion is fine; accumulating drift, NaNs, infinities and objects
exploding are not. Inspect warnings and investigate causes rather than blindly
running longer tests or changing parameters. Watch the evidence yourself and
refresh it when the implementation changes.

Measure performance against the 200x wall-clock-time / simulated-time limit.
Complete GAR CLI, HTTP and dashboard integration through the existing contract,
with usable task instructions, controls, observations and outcome evaluation.
Check the delivered entry points. Finish the report, screenshots and videos to
at least the seed environments' standard, with sources, equations, parameter
choices, experiments, results and honest limitations. Make the evidence easy
to find and reproduce with the code and commands you provide.

Keep code, research, evidence references and lessons in the shared workspace;
use the project's durable storage for large artifacts. Clean up your completed
simulation/render workers, preserve unrelated services, and use sensible waits
for long jobs. Follow the project's clean, pushed Git checkpoints and record
reusable lessons while they are fresh.

Read the four audit prompts in {audit_prompt_dir}.
The auditors can examine the code and evidence here; if their reports come
back, investigate the findings, fix causes and refresh affected evidence.
Finish with a handoff linking the implementation, report, evidence and runnable
commands, explaining what was actually checked and any remaining limitations.
Save your handoff to: {handoff_file}
