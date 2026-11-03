"""Operator-facing GAR commands; orchestration stays separate from presentation."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys

from .launch import LaunchSpec, describe, doctor
from .registry import available_environments
from .remote import RemoteEnv, RemoteError
from . import sessions, session_handoff, cli_sessions, cli_launch
from .cli_parser import parser, normalize, UsageError
from .cli_output import emit


def _spec(args):
    return cli_launch.launch(args)[0]


def _target(args):
    positional = getattr(args, "target", None)
    keyword = getattr(args, "session", None)
    if positional is not None and keyword is not None:
        raise ValueError("Supply TARGET or --session, not both.")
    return positional if positional is not None else keyword


def _client(args):
    value = _target(args)
    if value is not None:
        if args.url or args.key_file:
            raise ValueError("Use a session target OR --url with a key, not both.")
        return sessions.client(cli_sessions.choose(value), timeout_s=args.timeout)
    if not args.url:
        raise ValueError("Choose a session from 'gar sessions', or use --url with --key-file.")
    key = args.key_file.read_text().strip() if args.key_file else os.environ.get("GAR_HTTP_API_TOKEN")
    if not key:
        raise ValueError("External server access requires --key-file PATH or GAR_HTTP_API_TOKEN.")
    return RemoteEnv(args.url, api_key=key,
                     timeout_s=sessions.DEFAULT_CLIENT_TIMEOUT_S if args.timeout is None else args.timeout)


def _port_check(host, port):
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    if port == 0:
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as error:
        raise ValueError(f"Cannot listen on {host}:{port}: {error.strerror}. Check 'gar sessions'; use a different explicit --port if needed.") from error


def _error(error, command):
    value = {"ok": False, "error_type": type(error).__name__, "error": str(error)}
    if isinstance(error, json.JSONDecodeError):
        value["error"] = f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        value["hint"] = "Check quoting, or pass a JSON file with --file/--config."
    elif isinstance(error, RemoteError):
        payload = error.payload if isinstance(error.payload, dict) else {}
        message = payload.get("error", str(error))
        value["error"] = f"HTTP {error.status}: {message}"
        for name in ("request_id", "committed"):
            if name in payload:
                value[name] = payload[name]
        if error.status == 401:
            value["hint"] = "Check the current key with 'gar keys TARGET'."
        elif error.status == 403:
            value["hint"] = "This key cannot perform that operation. Use an owner key for privileged commands."
        elif command == "action":
            value["hint"] = ("No action executed; inspect 'gar action TARGET --help-actions'."
                             if payload.get("committed") is False else
                             "Do not retry blindly. Inspect the request/history with the owner; execution may have occurred.")
    elif isinstance(error, OSError) and command == "action":
        value["hint"] = "Execution may have occurred. Do not retry blindly; reconcile with the owner."
    elif isinstance(error, FileNotFoundError):
        value["hint"] = "Check the path; no missing file was created automatically."
    return value


def _requested_format(argv):
    for i, value in enumerate(argv):
        if value == "--format" and i+1 < len(argv):
            return argv[i+1]
        if value.startswith("--format="):
            return value.split("=", 1)[1]
    if "--json" in argv and ("action" not in argv or argv.index("--json") < argv.index("action")):
        return "json"
    return "text"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt, verbose, command = _requested_format(argv), False, None
    root = parser()
    try:
        args = root.parse_args(normalize(argv))
        fmt, verbose = getattr(args, "output_format", "text"), getattr(args, "verbose", False)
        command = args.command
        def show(kind, value):
            emit(kind, value, output_format=fmt, verbose=verbose)
        def progress(message):
            print(message, file=sys.stderr, flush=True)
        if command is None:
            root.print_help()
            return 0
        if command == "_serve":
            return sessions.serve(args.directory)
        if command in ("list", "envs"):
            show("environments", [describe(e["environment_id"], e["backend"]) for e in available_environments()])
        elif command == "inspect":
            show("inspect", describe(args.environment))
        elif command == "robots":
            from robots.catalog import list_models,list_setups,describe_component
            if args.operation in (None,"list"):
                show("robots",dict(models=list_models(),setups=list_setups()))
            else:
                show("robot",describe_component(args.name))
        elif command == "create":
            from .creation_audit.creator import from_args
            try:
                result = from_args(args, progress=progress)
            except KeyboardInterrupt:
                progress("Creator interrupted; research and logs retained.")
                return 130
            show("creator", result)
            return 0 if result["status"] == "turns_completed" else 1
        elif command == "create-audit":
            from .creation_audit.pipeline import from_args
            try:
                result = from_args(args, progress=progress)
            except KeyboardInterrupt:
                progress("Creation-audit pipeline interrupted; reports and logs retained.")
                return 130
            show("pipeline", result)
            return 0 if result["status"] == "completed" else 1
        elif command == "audit":
            from .creation_audit.method import from_args
            try:
                result = from_args(args, progress=progress)
            except KeyboardInterrupt:
                progress("Audit interrupted; partial reports and logs retained.")
                return 130
            show("audit", result)
            return 0 if result["status"] == "reports_written" else 1
        elif command == "doctor":
            result = doctor(LaunchSpec(args.environment))
            show("doctor", result)
            return 0 if result["ok"] else 1
        elif command == "tasks":
            from . import tasks
            operation = args.operation or "list"
            if operation == "list":
                show("catalogs", tasks.catalogs())
            elif operation == "inspect":
                show("catalog", tasks.catalog(args.environment))
            else:
                task = tasks.resolve(args.environment, cli_launch.selection(args, args.environment),
                    base=LaunchSpec.load(args.base_config) if args.base_config else None)
                value = task.to_dict()
                if args.output:
                    with args.output.open("x", encoding="utf-8") as stream:
                        json.dump(value, stream, indent=2, allow_nan=False)
                        stream.write("\n")
                    if fmt != "json":
                        value["saved_to"] = str(args.output)
                show("task", value)
        elif command in ("run", "serve"):
            if command == "run":
                session_handoff.validate_timeout(args.startup_timeout)
            spec, task = cli_launch.launch(args)
            _port_check(args.host, args.port)
            directory = sessions.prepare(spec, host=args.host, port=args.port, policy=args.policy_key, task=task)
            if command == "serve":
                # Backend startup chatter belongs in the log, not the operator view.
                stdout = sys.stdout
                progress(f"Starting {spec.environment_id} in foreground. Ctrl-C stops this server.")
                def ready():
                    result = session_handoff.describe(directory)
                    emit("launch", result, output_format=fmt, verbose=verbose, stream=stdout)
                with (directory / "server.log").open("a") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    code = sessions.serve(directory, ready_callback=ready)
                if code:
                    show("launch", session_handoff.describe(directory))
                return code
            try:
                receipt = sessions.start(directory)
            except OSError:
                show("launch", session_handoff.describe(directory))
                return 1
            if args.no_wait:
                show("starting", receipt)
            else:
                short = receipt["session_id"][:12]
                progress(f"Starting {spec.environment_id}...  Session {short}.  Logs: gar logs {short}")
                try:
                    result = session_handoff.wait(directory, timeout_s=args.startup_timeout,
                        pid=receipt["pid"], progress=lambda _: progress(f"Still starting {short}; gar logs {short} shows progress."))
                except KeyboardInterrupt:
                    progress(f"Stopped waiting; server was NOT stopped. Check: gar status {short}")
                    return 130
                show("launch", result)
                return 0 if result["ready"] else 1
        elif command in ("sessions", "status", "keys", "logs", "pause", "resume", "stop"):
            operation = (args.operation or "list") if command == "sessions" else command
            operation = {"credentials": "keys", "info": "status"}.get(operation, operation)
            value = getattr(args, "target", None)
            if operation == "list" or operation == "status" and value is None:
                show("sessions", cli_sessions.inventory(include_all=getattr(args, "all", False)))
                return 0
            directory = cli_sessions.choose(value)
            if operation == "status":
                result = session_handoff.describe(directory)
                result.pop("api_keys", None)
                show("status", result)
                return 0 if result["ready"] else 1
            if operation == "keys":
                manifest = json.loads((directory / "session.json").read_text())
                keys = json.loads(Path(manifest["launch"]["credential_file"]).read_text())
                show("keys", {"session_id": manifest["session_id"], **keys})
            elif operation == "logs":
                show("logs", cli_sessions.logs(directory, tail=args.tail))
            elif operation == "stop":
                progress(f"Stopping {directory.name[:12]}...")
                show("lifecycle", cli_sessions.stop(directory, timeout_s=args.timeout))
            elif operation in ("pause", "resume"):
                with sessions.client(directory) as remote:
                    show("lifecycle", {"operation": operation, **getattr(remote, operation)()})
        elif command == "benchmark":
            from .benchmark import run
            progress("Running bounded agent rollout; the session will be paused when it ends.")
            result = run(str(cli_sessions.choose(_target(args))), agent=args.agent, max_actions=args.max_actions,
                         output=args.output, resume=args.resume, timeout_s=args.timeout,
                         options=json.loads(args.agent_options_json))
            show("benchmark", {**result, "output_directory": str(args.output.resolve())})
            return 0 if result["status"] == "completed" else 2
        elif command == "export":
            from .evidence import export
            result = export(str(cli_sessions.choose(_target(args))), args.output, include_traces=args.include_traces)
            show("export", {**result, "output_directory": str(args.output.resolve())})
        elif command == "test":
            import unittest
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                "gym_anything_robotics.tests.test_session_contract",
                "gym_anything_robotics.tests.test_session_handoff",
                "gym_anything_robotics.tests.test_cli_ux",
                "gym_anything_robotics.tests.test_tasks",
                "gym_anything_robotics.tests.test_benchmark",
                "gym_anything_robotics.tests.test_http_action_rejection",
                "gym_anything_robotics.tests.test_laundry_action_rejection"])
            buffer = io.StringIO()
            result = unittest.TextTestRunner(stream=buffer, verbosity=2 if verbose else 0).run(suite)
            if verbose or not result.wasSuccessful():
                print(buffer.getvalue(), file=sys.stderr)
            show("test", {"ok": result.wasSuccessful(), "tests_run": result.testsRun,
                           "failures": len(result.failures), "errors": len(result.errors)})
            return 0 if result.wasSuccessful() else 1
        else:
            with _client(args) as remote:
                if command == "verify":
                    result = remote.evaluate()
                    show("evaluation", result)
                    return (0 if result["success"] else 1) if result["available"] else 2
                if command in ("observe", "observation"):
                    show("observation", {**remote.obs(), "server_url": remote.url})
                elif command == "state":
                    value = remote.get_state()
                    if args.field:
                        try:
                            for part in args.field.split("."):
                                value = value[int(part)] if isinstance(value, list) else value[part]
                        except (KeyError, IndexError, TypeError, ValueError) as error:
                            raise ValueError(f"No state field '{args.field}'. Inspect 'gar state TARGET --format json' for exact paths.") from error
                        if fmt == "json":
                            show("state", value)
                        else:
                            show("state", value if isinstance(value, dict) else {args.field: value})
                    else:
                        show("state", value)
                elif command == "action":
                    if args.help_actions:
                        if args.action_json is not None or args.file:
                            raise ValueError("--help-actions cannot be combined with an action payload.")
                        help_value = remote.request("/v1/help")
                        caps = remote.request("/v1/capabilities")["capabilities"]
                        help_value["action_types"] = {name: info for name, info in help_value["action_types"].items()
                                                     if name in caps["action_types"]}
                        help_value["effective_capabilities"] = caps
                        show("actions", help_value)
                    else:
                        if args.action_json is None and args.file is None:
                            raise ValueError("Supply --file action.json or --data JSON. For permitted actions: gar action TARGET --help-actions")
                        body = args.file.read_text() if args.file else args.action_json
                        response = remote.act(json.loads(body), idempotency_key=args.idempotency_key)
                        show("action", response)
                        return 0 if response.get("ok") is True and response.get("committed") is True else 1
                elif command == "dashboard":
                    show("dashboard", {"url": remote.url + "/dashboard"})
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        emit("error", _error(error, command), output_format=fmt if fmt in ("text", "json") else "text",
             verbose=verbose, stream=sys.stderr)
        return 2 if isinstance(error, UsageError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
