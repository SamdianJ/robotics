"""Thin shared-workspace auditor launcher; see README.md for the pinned reference."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

AUDITORS = ("qualitative", "evidence", "infrastructure", "hack")
DEFAULT_TIMEOUT_SEC = 7200
DEFAULT_BACKEND = "cc"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_CLAUDE_EFFORT = "xhigh"
DISALLOWED_TOOLS = "AskUserQuestion,EnterPlanMode,ExitPlanMode,Task(Plan)"
MEMORY = Path(__file__).resolve().parent / "memory"


def _model_for(backend, model):
    """Pin Claude by default without sending a Claude model to explicit Codex runs."""
    return model if model is not None else (DEFAULT_CLAUDE_MODEL if backend == "cc" else None)


def _resolve_bin(explicit, env_var, name):
    candidate = explicit or os.environ.get(env_var) or name
    resolved = shutil.which(candidate)
    if not resolved:
        raise RuntimeError(f"Cannot find {name}; install it or pass --{name}-bin PATH.")
    return Path(resolved).resolve()


def _kill_process_group(pgid):
    """Release only children of this invocation, never other shared-space jobs."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_agent(binary, args, *, workspace, timeout, log):
    """Stream to disk; timeout/nonzero exit is a failed invocation, never success."""
    with log.open("x", encoding="utf-8") as output, log.with_suffix(".stderr.log").open(
        "x", encoding="utf-8"
    ) as errors:
        proc = subprocess.Popen(
            [str(binary), *args], cwd=workspace, stdin=subprocess.DEVNULL,
            stdout=output, stderr=errors, start_new_session=True,
        )
        try:
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"Agent timed out after {timeout}s; logs: {log}") from error
            if code:
                raise RuntimeError(f"Agent exited with code {code}; logs: {log}")
        finally:
            _kill_process_group(proc.pid)
            proc.wait()


def _invoke(backend, binary, prompt, *, session_id, resume, workspace, timeout, log, model):
    from gym_anything_robotics.provider_limits import invoke_with_recovery
    return invoke_with_recovery(_invoke_once, backend, binary, prompt,
        session_id=session_id, resume=resume, workspace=workspace,
        timeout=timeout, log=log, model=model)


def _invoke_once(backend, binary, prompt, *, session_id, resume, workspace, timeout, log, model):
    if backend == "cc":
        args = ["-p", prompt, "--dangerously-skip-permissions",
                "--disallowedTools", DISALLOWED_TOOLS, "--output-format", "json"]
        args += ["--resume" if resume else "--session-id", session_id]
        args += ["--effort", DEFAULT_CLAUDE_EFFORT]
    else:
        args = ["--yolo", "exec"]
        if resume:
            args += ["resume", session_id]
        args += ["--json", prompt]
    if model:
        args += ["--model", model]
    _run_agent(binary, args, workspace=workspace, timeout=timeout, log=log)
    if backend == "cc":
        result = json.loads(log.read_text(encoding="utf-8"))
        if result.get("is_error") or result.get("subtype") != "success":
            raise RuntimeError(f"Claude did not complete successfully; logs: {log}")
        if result.get("session_id") != session_id:
            raise RuntimeError(f"Claude returned a different session; logs: {log}")
        return session_id
    # Codex chooses the fresh ID; stdout prose is NOT a session ID.
    thread_id, completed, failed, errors = None, False, False, 0
    with log.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            elif event.get("type") == "turn.started":
                completed = False
            elif event.get("type") == "turn.completed":
                completed = True
            elif event.get("type") == "turn.failed":
                failed = True
            elif event.get("type") == "error":
                # Reconnection errors can recover within the same CLI turn.
                # Require a subsequent terminal completion, retaining the log.
                completed = False
                errors += 1
    if failed or not completed or not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError(f"Codex did not return a completed turn and thread ID; logs: {log}")
    if resume and thread_id != session_id:
        raise RuntimeError(f"Codex resumed a different session; logs: {log}")
    if errors:
        print(f"Codex recovered after {errors} error event(s); original events retained: {log}",
              file=sys.stderr, flush=True)
    return thread_id


def _audit_explore_prompt():
    return ("deep explore this repository to understand what it is about, "
            "how each individual components work, etc")


def _audit_run_prompt(prompt_file, env_dir, audit_file, *, video_note=""):
    text = (f"read @{prompt_file} and follow the prompt. "
            f"target env directory is @{env_dir}. "
            f"Substitute {{env_dir}} with {env_dir} and {{audit_file}} with {audit_file}. "
            f"Note: save file is {audit_file}")
    return text + ("\n\n" + video_note if video_note else "")


def _video_note():
    import importlib.util
    try:
        installed = importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        installed = False
    if installed and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return ("Optional video API: read @" + str(MEMORY / "video_tool.md") +
                ". The helper uses Gemini 3.8 Flash; API availability is checked when called.")
    return ("The optional Gemini video helper is not configured in this process "
            "(requires google-genai and GEMINI_API_KEY or GOOGLE_API_KEY). "
            "Do not claim to have used it. Direct evidence inspection is still available.")


def run_audits(*, env_dir, workspace, auditors=AUDITORS, backend=DEFAULT_BACKEND, model=None,
               output=None, memory_dir=None, claude_bin=None, codex_bin=None,
               timeout_sec=DEFAULT_TIMEOUT_SEC, progress=None, check_plan=None):
    if os.name != "posix":
        raise RuntimeError("Run the auditors on Linux or macOS.")
    if backend not in ("cc", "codex"):
        raise ValueError("Backend must be cc or codex.")
    if timeout_sec <= 0:
        raise ValueError("Timeout must be positive.")
    model = _model_for(backend, model)
    workspace = Path(workspace).resolve(strict=True)
    env_dir = Path(env_dir)
    env_dir = (env_dir if env_dir.is_absolute() else workspace / env_dir).resolve(strict=True)
    if not workspace.is_dir() or not env_dir.is_dir():
        raise ValueError("Workspace and environment must be directories.")
    auditors = tuple(dict.fromkeys(auditors))
    if not auditors or any(a not in AUDITORS for a in auditors):
        raise ValueError(f"Choose auditors from {AUDITORS}.")
    memory_dir = Path(memory_dir or MEMORY).resolve(strict=True)
    prompts = {a: memory_dir / f"{a}_prompt.md" for a in auditors}
    for path in prompts.values():
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise ValueError(f"Missing or empty prompt: {path}")
    binary = (_resolve_bin(claude_bin, "CLAUDE_BIN", "claude") if backend == "cc"
              else _resolve_bin(codex_bin, "CODEX_BIN", "codex"))
    if output is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        output = workspace / "artifacts" / "audits" / f"{env_dir.name}-{stamp}-{uuid.uuid4().hex[:8]}"
    else:
        output = Path(output)
        if not output.is_absolute():
            output = workspace / output
    output = output.resolve()
    # Each invocation keeps its own reports, NOT a separate workspace/check-out.
    output.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "workspace": str(workspace), "env_dir": str(env_dir),
              "backend": backend, "model": model, "output_directory": str(output), "auditors": {},
              "effort": DEFAULT_CLAUDE_EFFORT if backend == "cc" else None}
    manifest = output / "run.json"

    def save():
        manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    save()
    progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    progress(f"Auditing {env_dir}\nReports and logs: {output}")
    try:
        # One fixed probe per candidate version, shared as evidence by all four roles.
        # A failed check is feedback to repair, not a reason to suppress the auditors.
        plan = Path(check_plan) if check_plan is not None else env_dir / "checks.json"
        if not plan.is_absolute():
            plan = workspace / plan
        checks_note = "Fixed checks were not configured; do not claim they passed."
        if check_plan is not None or plan.is_file():
            from gym_anything_robotics.checks import run_checks
            checked = run_checks(plan=plan, output=output / "checks", workspace=workspace, progress=progress)
            report_path = output / "checks" / "results.json"
            result["checks"] = dict(status=checked["status"], report=str(report_path))
            checks_note = (f"Fixed deterministic checks ran before this audit. Read @{report_path}. "
                           "Treat failures and skipped coverage explicitly. Reuse the retained measurements; "
                           "do not rewrite the standard probe. Add bounded scene-specific checks where needed. "
                           "These checks do not establish physical fidelity or visual correctness.")
        else:
            result["checks"] = dict(status="not_configured")
        save()
        for auditor in auditors:
            directory = output / auditor
            directory.mkdir()
            report = directory / "audit.md"
            # Keep exactly the prompt that this run was given.
            prompt_file = directory / "prompt.md"
            prompt_file.write_text(prompts[auditor].read_text(encoding="utf-8"), encoding="utf-8")
            session = str(uuid.uuid4()) if backend == "cc" else None
            entry = {"status": "exploring", "session_id": session, "report": str(report)}
            result["auditors"][auditor] = entry
            save()
            try:
                progress(f"{auditor}: exploring")
                session = _invoke(
                    backend, binary, _audit_explore_prompt(), session_id=session, resume=False,
                    workspace=workspace, timeout=timeout_sec, log=directory / "explore.jsonl",
                    model=model,
                )
                entry.update(session_id=session, status="auditing")
                save()
                progress(f"{auditor}: auditing")
                session = _invoke(
                    backend, binary, _audit_run_prompt(
                        prompt_file, env_dir, report,
                        video_note=checks_note + ("\n" + _video_note() if auditor == "evidence" else ""),
                    ), session_id=session, resume=True, workspace=workspace, timeout=timeout_sec,
                    log=directory / "audit.jsonl", model=model,
                )
                if not report.is_file() or not report.read_text(encoding="utf-8").strip():
                    raise RuntimeError(f"Agent produced no nonempty audit report: {report}")
                entry["status"] = "report_written"
                progress(f"{auditor}: report written — {report}")
            except (OSError, ValueError, RuntimeError) as error:
                entry.update(status="failed", error=str(error))
                progress(f"{auditor}: FAILED — {error}")
            save()
        result["status"] = ("reports_written" if all(
            e["status"] == "report_written" for e in result["auditors"].values()) else "failed")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        result.update(status="failed", error=str(error))
        progress(f"Audit setup/checks FAILED: {error}")
    except BaseException:
        result["status"] = "interrupted"
        for entry in result["auditors"].values():
            if entry["status"] in ("exploring", "auditing"):
                entry["status"] = "interrupted"
        raise
    finally:
        save()
    return result


def add_arguments(parser):
    parser.add_argument("--env-dir", type=Path, required=True, help="Environment source/evidence directory")
    parser.add_argument("--auditor", choices=AUDITORS, action="append",
                        help="Run one auditor; repeat to select several (default: all four)")
    parser.add_argument("--backend", choices=("cc", "codex"), default=DEFAULT_BACKEND,
                        help="Coding-agent CLI: cc = Claude Code (default) or codex")
    parser.add_argument("--model", help="Exact model override (default: claude-opus-5-5 for cc; CLI config for explicit codex)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Shared checkout (default: current directory)")
    parser.add_argument("--output", type=Path, help="New report/log directory; never overwrites an existing run")
    parser.add_argument("--memory-dir", type=Path, help="Override the four Markdown prompt files")
    parser.add_argument("--check-plan", type=Path,
                        help="Fixed checks plan (default: ENV_DIR/checks.json when present)")
    parser.add_argument("--claude-bin", help="Claude CLI executable path")
    parser.add_argument("--codex-bin", help="Codex CLI executable path")
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC,
                        help="Timeout per invocation (default: 7200 seconds)")


def from_args(args, progress=None):
    # SIGTERM follows the same cleanup path as Ctrl-C instead of orphaning children.
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    handling = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, interrupt) if handling else None
    try:
        return run_audits(
            env_dir=args.env_dir, workspace=args.workspace, auditors=args.auditor or AUDITORS,
            backend=args.backend, model=args.model, output=args.output, memory_dir=args.memory_dir,
            claude_bin=args.claude_bin, codex_bin=args.codex_bin, timeout_sec=args.timeout_sec,
            progress=progress,
            check_plan=args.check_plan,
        )
    finally:
        if handling:
            signal.signal(signal.SIGTERM, previous)


def run(argv=None):
    parser = argparse.ArgumentParser(description="Run the four shared-workspace robotics auditors.")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = from_args(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Audit launch failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Audit interrupted; partial reports and logs retained.", file=sys.stderr)
        return 130
    print(f"Audits: {result['status']}\nReports: {result['output_directory']}")
    print("Report generation is not an environment pass; read the auditors' findings.")
    return 0 if result["status"] == "reports_written" else 1


if __name__ == "__main__":
    raise SystemExit(run())
