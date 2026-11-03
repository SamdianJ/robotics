"""Public command grammar and examples. No environment imports or execution."""
import argparse
from pathlib import Path

HELP = """Operate persistent robotics environments.

Discover       list, inspect, robots, tasks, doctor
Build          create, create-audit
Start/connect  run, serve, sessions, status, keys, dashboard
Operate        action, pause, resume, stop
Inspect        observe, state, logs
Evaluate       verify, benchmark, export, test, audit

Examples:
  gar run laundry --task shirt --port 8896 --policy-key
  gar sessions
  gar status laundry
  gar dashboard laundry
  gar stop laundry

Targets: an unambiguous environment name, a short session ID, or a full ID.
Text is the default. Use --format json for scripts; --verbose for diagnostics.
"""

class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def _check_value(self, action, value):
        if action.dest in ("command", "operation") and value not in action.choices:
            from difflib import get_close_matches
            names = [name for name in action.choices if not name.startswith("_")]
            match = get_close_matches(value, names, n=1)
            hint = f" Did you mean '{self.prog} {match[0]}'?" if match else ""
            raise UsageError(f"Unknown {action.dest} '{value}'.{hint} See '{self.prog} --help'.")
        return super()._check_value(action, value)

    def format_help(self):
        if self.prog == "gar":
            return "Usage: gar COMMAND ...\n\n" + HELP + "\nOptions: --format {text,json}, --json, --verbose\nDetails: gar COMMAND --help\n"
        return super().format_help()

    def error(self, message):
        raise UsageError(f"{message}\nSee '{self.prog} --help'.")


def display(p, *, json_alias=True):
    p.add_argument("--format", dest="output_format", choices=("text", "json"),
                   default=argparse.SUPPRESS, help="Output format (default: text)")
    if json_alias:
        p.add_argument("--json", dest="output_format", action="store_const", const="json",
                       default=argparse.SUPPRESS, help="Machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="Additional diagnostics")


def selectors(p):
    p.add_argument("--task", help="Named task; see 'gar tasks ENV'")
    p.add_argument("--variation", type=int, help="Exact catalogue variation ID")
    p.add_argument("--select", action="append", default=[], metavar="KEY=VALUE",
                   help="Task selector; repeat for multiple fields")
    p.add_argument("--selector-json", help="Advanced: JSON task selectors")


def target(p, *, remote=True):
    p.add_argument("target", nargs="?", metavar="TARGET", help="Environment name, short ID, full ID or directory")
    if remote:
        p.add_argument("--session", help="Compatibility spelling for TARGET")
        p.add_argument("--url", help="Attach to an external HTTP server")
        p.add_argument("--key-file", type=Path,
                       help="Bearer token file for --url; alternatively GAR_HTTP_API_TOKEN")
        p.add_argument("--timeout", type=float, default=None,
                       help="HTTP timeout in seconds (default: the session's declared client timeout, else 120)")


def parser():
    root = Parser(prog="gar", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    display(root)
    commands = root.add_subparsers(dest="command", metavar="COMMAND")
    def command(name, help, **kwargs):
        p = commands.add_parser(name, help=help, description=help, **kwargs)
        display(p, json_alias=name != "action")
        return p
    command("list", "List available environments (does not start physics)", aliases=["envs"])
    p=command("robots","Discover shared robots and deployment presets; no physics startup")
    ops=p.add_subparsers(dest="operation")
    sub=ops.add_parser("list",help="List robot models and presets")
    display(sub)
    sub=ops.add_parser("inspect",help="Inspect a model or deployment preset")
    sub.add_argument("name");display(sub)
    for name, desc in (("inspect", "Show an environment's robot, engine and launch requirements"),
                       ("doctor", "Check runtime dependencies; does not simulate")):
        command(name, desc).add_argument("environment")
    for name, desc in (("run", "Start a background server and wait until ready"),
                       ("serve", "Run a foreground server; Ctrl-C stops this server")):
        p = command(name, desc)
        p.add_argument("environment", nargs="?")
        selectors(p)
        p.add_argument("--host", default="127.0.0.1", help="Listen address (default: local only)")
        p.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080; 0 allocates a free port)")
        p.add_argument("--policy-key", action="store_true", help="Enable a restricted client key as well as the owner key")
        p.add_argument("--device", help="Exact device override")
        advanced = p.add_argument_group("Advanced launch configuration")
        advanced.add_argument("--config", type=Path, help="Exact gar.launch.v1 file")
        advanced.add_argument("--task-file", type=Path, help="Resolved gar.task.v1 file")
        advanced.add_argument("--backend-options-json", help="Exact backend constructor options")
        advanced.add_argument("--backend", help="Backend override")
        advanced.add_argument("--robot", help="Robot override")
        if name == "run":
            p.add_argument("--startup-timeout", type=float, default=600, help="Readiness wait in seconds (default: 600)")
            p.add_argument("--no-wait", action="store_true", help="Return before startup finishes (automation)")
    p = command("sessions", "List active sessions; --all includes historical sessions")
    p.add_argument("--all", action="store_true", default=argparse.SUPPRESS, help="Include stopped, failed and exited sessions")
    ops = p.add_subparsers(dest="operation", metavar="OPERATION")
    for name in ("list", "status", "info", "credentials", "pause", "resume", "stop", "logs"):
        sub = ops.add_parser(name, help=f"Compatibility: {name}")
        display(sub)
        if name == "list":
            sub.add_argument("--all", action="store_true", default=argparse.SUPPRESS)
        else:
            target(sub, remote=False)
            if name == "stop":
                sub.add_argument("--timeout", type=float, default=30, help="Seconds to wait for graceful shutdown")
            if name == "logs":
                sub.add_argument("--tail", type=int, default=40)
    p = command("tasks", "Browse task catalogues or resolve an exact task manifest")
    ops = p.add_subparsers(dest="operation", metavar="OPERATION")
    sub = ops.add_parser("list", help="List task catalogues")
    display(sub)
    sub = ops.add_parser("inspect", help="Show one environment's task choices and selectors")
    display(sub)
    sub.add_argument("environment")
    sub = ops.add_parser("resolve", help="Resolve a task without starting physics")
    display(sub)
    sub.add_argument("environment")
    selectors(sub)
    sub.add_argument("--base-config", type=Path)
    sub.add_argument("--output", type=Path, help="Write a reusable task manifest; will not overwrite")
    for name, desc in (("status", "Show live session status; no target lists active sessions"),
                       ("keys", "Show a session's API keys (secret output)"),
                       ("pause", "Pause command admission; preserve live state"),
                       ("resume", "Allow commands again; does not advance or reset physics"),
                       ("stop", "Gracefully shut down the selected server and simulator"),
                       ("logs", "Show recent server logs (keys redacted)")):
        p = command(name, desc)
        target(p, remote=False)
        if name == "stop":
            p.add_argument("--timeout", type=float, default=30, help="Seconds to wait for graceful shutdown")
        if name == "logs":
            p.add_argument("--tail", type=int, default=40, help="Number of most recent lines")
    for name, desc in (("observe", "Read current cameras and robot feedback; never advances physics"),
                       ("state", "Read a concise privileged state summary"),
                       ("action", "Send one explicit action request; never auto-retries"),
                       ("dashboard", "Get the dashboard URL for the same live session"),
                       ("verify", "Check task success with the environment's evaluator")):
        kwargs = {"aliases": ["observation"]} if name == "observe" else {}
        p = command(name, desc, **kwargs)
        target(p)
        if name == "state":
            p.add_argument("--field", help="Inspect a dotted state path, e.g. joints or garment.mass_kg")
        if name == "action":
            value = p.add_mutually_exclusive_group()
            value.add_argument("--data", "--json", dest="action_json", help="Action JSON (legacy --json is an INPUT flag)")
            value.add_argument("--file", type=Path, help="Action JSON file")
            p.add_argument("--help-actions", action="store_true", help="Read permitted actions from the live server; execute nothing")
            p.add_argument("--idempotency-key", help="Caller-owned request key; retain it for reconciliation")
    p = command("benchmark", "Run a bounded restricted-agent rollout; no reset or retry")
    target(p, remote=False)
    p.add_argument("--session", help="Compatibility spelling for TARGET")
    p.add_argument("--agent", required=True, help="Trusted local module:factory")
    p.add_argument("--max-actions", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--agent-options-json", default="{}")
    p.add_argument("--resume", action="store_true", help="Explicitly resume the selected paused session")
    p.add_argument("--timeout", type=float, default=None,
                   help="HTTP timeout per request in seconds (default: the session's declared client timeout, else 120)")
    p = command("export", "Export retained history/trace evidence (not a video)")
    target(p, remote=False)
    p.add_argument("--session", help="Compatibility spelling for TARGET")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--include-traces", action="store_true")
    from .creation_audit.creator import add_arguments as creator_arguments
    creator_arguments(command("create", "Research, build, then blind self-review with a coding agent (not a simulator launch)"))
    from .creation_audit.method import add_arguments as audit_arguments
    audit_arguments(command("audit", "Run shared-workspace coding auditors; reports are not a pass verdict"))
    from .creation_audit.pipeline import add_arguments as pipeline_arguments
    pipeline_arguments(command("create-audit", "Create, audit, then run three Creator repair/re-audit cycles"))
    p = command("test", "Run interface contract checks (not physical qualification)")
    p.add_argument("--suite", choices=("contract",), default="contract")
    # Private worker is parsed but deliberately absent from the public menu.
    commands.add_parser("_serve", help=argparse.SUPPRESS).add_argument("directory")
    commands._choices_actions = [a for a in commands._choices_actions if a.dest != "_serve"]
    return root


def normalize(argv):
    values = list(argv)
    # 'gar tasks laundry' is a readable alias for 'gar tasks inspect laundry'.
    if "tasks" in values:
        i = values.index("tasks")
        if i + 1 < len(values) and not values[i + 1].startswith("-") and values[i + 1] not in ("list", "inspect", "resolve"):
            values.insert(i + 1, "inspect")
    return values
