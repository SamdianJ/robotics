"""Research, build, then a blind nudge in the same shared coding-agent session."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from . import method

PROMPTS = method.MEMORY / "env_creation_notes"
CONTEXT = method.MEMORY / "environment_guide.md"


def _path(value, workspace):
    value = Path(value)
    return (value if value.is_absolute() else workspace / value).resolve()


def run_creator(*, action_group, env_dir, workspace, backend=method.DEFAULT_BACKEND, model=None,
                output=None, prompt_dir=None, audit_prompt_dir=None, claude_bin=None, codex_bin=None,
                timeout_sec=method.DEFAULT_TIMEOUT_SEC, progress=None):
    if os.name != "posix":
        raise RuntimeError("Run the creator on Linux or macOS.")
    if backend not in ("cc", "codex"):
        raise ValueError("Backend must be cc or codex.")
    if timeout_sec <= 0:
        raise ValueError("Timeout must be positive.")
    model = method._model_for(backend, model)
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory.")
    group = _path(action_group, workspace)
    if not (group.is_file() or group.is_dir()):
        raise ValueError("Action group must reference an existing record file or directory.")
    target = _path(env_dir, workspace)
    if target == workspace or not target.is_relative_to(workspace):
        raise ValueError("Choose an environment directory inside the shared checkout.")
    if target.exists() and not target.is_dir():
        raise ValueError("Environment target is not a directory.")
    prompts = _path(prompt_dir or PROMPTS, workspace)
    audit_prompts = _path(audit_prompt_dir or method.MEMORY, workspace)
    if not prompts.is_dir():
        raise ValueError(f"Missing prompt directory: {prompts}")
    binary = (method._resolve_bin(claude_bin, "CLAUDE_BIN", "claude") if backend == "cc"
              else method._resolve_bin(codex_bin, "CODEX_BIN", "codex"))
    if output is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        output = workspace / "artifacts" / "creations" / f"{target.name}-{stamp}-{uuid.uuid4().hex[:8]}"
    output = _path(output, workspace)
    output.mkdir(parents=True, exist_ok=False)
    result = {"status": "researching", "workspace": str(workspace),
              "action_group": str(group), "env_dir": str(target), "backend": backend,
              "model": model, "output_directory": str(output), "session_id": None,
              "effort": method.DEFAULT_CLAUDE_EFFORT if backend == "cc" else None,
              "turns": {}}
    manifest = output / "run.json"
    research = output / "research.md"
    active = None

    def save():
        temporary = output / "run.json.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manifest)

    progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    save()
    progress(f"Creator target: {target}\nResearch and logs: {output}")

    def turn(name, report, *, resume):
        nonlocal active
        active = {"status": "running", "report": str(report)}
        result["turns"][name] = active
        save()
        # Load and materialize only this turn. In particular, no build request
        # or build artifact directory exists during the research invocation.
        template = (prompts / f"{name}_prompt.md").read_text(encoding="utf-8")
        if not template.strip():
            raise ValueError(f"Empty {name} prompt.")
        request = template.format(action_group=str(group), env_dir=str(target),
                                  research_file=str(research), handoff_file=str(report),
                                  context_file=str(CONTEXT), audit_prompt_dir=str(audit_prompts))
        directory = output / name
        directory.mkdir()
        (directory / "prompt.md").write_text(request, encoding="utf-8")
        progress(f"Creator: {name}")
        session = result["session_id"]
        if not resume and backend == "cc":
            session = str(uuid.uuid4())
        session = method._invoke(
            backend, binary, request, session_id=session, resume=resume,
            workspace=workspace, timeout=timeout_sec, log=directory / "turn.jsonl", model=model,
        )
        result["session_id"] = session
        active["session_id"] = session
        if not report.is_file() or not report.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"Creator produced no nonempty {name} report: {report}")
        # Preserve the first build handoff even if the nudge revises the shared
        # handoff. These are records, not an independent acceptance verdict.
        (directory / report.name).write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
        active["status"] = "completed"
        save()
        active = None

    try:
        turn("research", research, resume=False)
        result["status"] = "building"
        save()
        turn("build", output / "handoff.md", resume=True)
        result["status"] = "nudging"
        save()
        turn("nudge", output / "handoff.md", resume=True)
        result["status"] = "turns_completed"
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        result.update(status="failed", error=str(error))
        if active is not None:
            active.update(status="failed", error=str(error))
        progress(f"Creator FAILED: {error}")
    except BaseException:
        result["status"] = "interrupted"
        if active is not None:
            active["status"] = "interrupted"
        raise
    finally:
        save()
    return result


def add_arguments(parser):
    parser.add_argument("--action-group", type=Path, required=True,
                        help="Complete grounded action-group record file/directory (not just a label)")
    parser.add_argument("--env-dir", type=Path, required=True,
                        help="Environment source directory to create or improve in the shared checkout")
    parser.add_argument("--backend", choices=("cc", "codex"), default=method.DEFAULT_BACKEND,
                        help="Coding-agent CLI (default: cc = Claude Code, using existing login)")
    parser.add_argument("--model", help="Exact model override (default: claude-opus-5-5 for cc; CLI config for explicit codex)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Shared checkout")
    parser.add_argument("--output", type=Path, help="New research/handoff/log directory; never overwritten")
    parser.add_argument("--prompt-dir", type=Path, help="Directory containing research_prompt.md, build_prompt.md and nudge_prompt.md")
    parser.add_argument("--claude-bin", help="Claude CLI executable path")
    parser.add_argument("--codex-bin", help="Codex CLI executable path")
    parser.add_argument("--timeout-sec", type=int, default=method.DEFAULT_TIMEOUT_SEC,
                        help="Timeout per turn (default: 7200 seconds)")


def from_args(args, progress=None):
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    handling = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, interrupt) if handling else None
    try:
        return run_creator(
            action_group=args.action_group, env_dir=args.env_dir, workspace=args.workspace,
            backend=args.backend, model=args.model, output=args.output, prompt_dir=args.prompt_dir,
            claude_bin=args.claude_bin, codex_bin=args.codex_bin, timeout_sec=args.timeout_sec,
            progress=progress,
        )
    finally:
        if handling:
            signal.signal(signal.SIGTERM, previous)


def run(argv=None):
    parser = argparse.ArgumentParser(description="Research, build, then a blind nudge in the same shared-workspace creator session.")
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        result = from_args(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Creator launch failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Creator interrupted; research and logs retained.", file=sys.stderr)
        return 130
    from ..cli_output import render
    print(render("creator", result))
    return 0 if result["status"] == "turns_completed" else 1


if __name__ == "__main__":
    raise SystemExit(run())
