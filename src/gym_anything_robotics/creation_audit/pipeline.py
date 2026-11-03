"""Create once, then run bounded audit-feedback/repair/re-audit cycles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from . import creator, method


def run_pipeline(*, action_group, env_dir, workspace, rounds=3,
                 backend=method.DEFAULT_BACKEND, model=None, output=None,
                 prompt_dir=None, memory_dir=None, claude_bin=None, codex_bin=None,
                 timeout_sec=method.DEFAULT_TIMEOUT_SEC, progress=None):
    """An initial audit plus ``rounds`` repairs, each followed by a fresh full audit.

    Completion means the scheduled calls and reports completed, not that the
    environment passed independent physical qualification. Failed audits stop
    the pipeline; partial reports are never silently fed to a repair.
    """
    if os.name != "posix":
        raise RuntimeError("Run the creation-audit pipeline on Linux or macOS.")
    if type(rounds) is not int or rounds < 1:
        raise ValueError("Rounds must be a positive integer.")
    if backend not in ("cc", "codex") or timeout_sec <= 0:
        raise ValueError("Choose backend cc/codex and a positive timeout.")
    workspace = Path(workspace).resolve(strict=True)
    group = creator._path(action_group, workspace)
    target = creator._path(env_dir, workspace)
    if not workspace.is_dir() or not (group.is_file() or group.is_dir()):
        raise ValueError("Workspace and action-group record must exist.")
    if target == workspace or not target.is_relative_to(workspace):
        raise ValueError("Choose an environment directory inside the shared checkout.")
    if target.exists() and not target.is_dir():
        raise ValueError("Environment target is not a directory.")
    prompts = creator._path(prompt_dir or creator.PROMPTS, workspace)
    memory = creator._path(memory_dir or method.MEMORY, workspace)
    # Catch missing instructions before starting paid agent calls.
    for path in [*(prompts / f"{name}_prompt.md" for name in ("research", "build", "nudge", "repair")),
                 *(memory / f"{name}_prompt.md" for name in method.AUDITORS), creator.CONTEXT]:
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise ValueError(f"Missing or empty instruction file: {path}")
    model = method._model_for(backend, model)
    binary = (method._resolve_bin(claude_bin, "CLAUDE_BIN", "claude") if backend == "cc"
              else method._resolve_bin(codex_bin, "CODEX_BIN", "codex"))
    if output is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        output = workspace / "artifacts" / "pipelines" / f"{target.name}-{stamp}-{uuid.uuid4().hex[:8]}"
    output = creator._path(output, workspace)
    output.mkdir(parents=True, exist_ok=False)
    result = dict(schema="gar.creation-audit-pipeline.v1", status="running", stage="creation",
                  workspace=str(workspace), env_dir=str(target), action_group=str(group),
                  backend=backend, model=model, rounds_requested=rounds, rounds_completed=0,
                  output_directory=str(output), creator_session_id=None, rounds=[],
                  qualification="Reports are evidence for review, not an environment pass verdict.")
    progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    active = None

    def save():
        temporary = output / "run.json.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "run.json")

    common = dict(env_dir=target, workspace=workspace, backend=backend, model=model,
                  claude_bin=claude_bin, codex_bin=codex_bin,
                  timeout_sec=timeout_sec, progress=progress)

    def audit(directory):
        reports = method.run_audits(output=directory, memory_dir=memory, **common)
        if reports["status"] != "reports_written":
            raise RuntimeError(f"Audit did not complete; inspect {directory / 'run.json'}")
        # Preserve exact report content and its provenance in the next feedback packet.
        sources = []
        for role in method.AUDITORS:
            entry = reports["auditors"].get(role, {})
            if entry.get("status") != "report_written":
                raise RuntimeError(f"Missing completed {role} audit in {directory}")
            report = Path(entry["report"])
            data = report.read_bytes()
            if not data.strip():
                raise RuntimeError(f"Empty audit report: {report}")
            sources.append(dict(auditor=role, report=str(report),
                                sha256=hashlib.sha256(data).hexdigest(), text=data.decode("utf-8")))
        return sources

    save()
    try:
        result["creation_manifest"] = str(output / "creation" / "run.json")
        save()
        created = creator.run_creator(action_group=group, output=output / "creation",
                                      prompt_dir=prompts, audit_prompt_dir=memory, **common)
        result["creator_session_id"] = created.get("session_id")
        if created["status"] != "turns_completed" or not result["creator_session_id"]:
            raise RuntimeError(f"Creator did not complete: {created.get('error', created['status'])}")
        if not target.is_dir():
            raise RuntimeError(f"Creator did not create environment directory: {target}")
        result["stage"] = "initial_audit"
        result["initial_audit"] = str(output / "initial-audit" / "run.json")
        save()
        reports = audit(output / "initial-audit")
        for number in range(1, rounds + 1):
            directory = output / f"round-{number:02d}"
            directory.mkdir()
            feedback = directory / "feedback.md"
            feedback.write_text("# Independent audit feedback\n\n" + "\n\n".join(
                f"## {r['auditor']}\nSource: {r['report']}\nSHA-256: {r['sha256']}\n\n{r['text']}"
                for r in reports) + "\n", encoding="utf-8")
            handoff = directory / "repair-handoff.md"
            active = dict(round=number, status="repairing", feedback=str(feedback),
                          sources=[{k: v for k, v in r.items() if k != "text"} for r in reports],
                          handoff=str(handoff), audit_manifest=str(directory / "audit" / "run.json"))
            result["rounds"].append(active)
            result["stage"] = f"round_{number}_repair"
            save()
            request = (prompts / "repair_prompt.md").read_text(encoding="utf-8").format(
                round_number=number, rounds=rounds, action_group=str(group), env_dir=str(target),
                feedback_file=str(feedback), handoff_file=str(handoff),
                context_file=str(creator.CONTEXT), audit_prompt_dir=str(memory))
            (directory / "repair-prompt.md").write_text(request, encoding="utf-8")
            progress(f"Pipeline round {number}/{rounds}: resume Creator to repair audit findings")
            session = method._invoke(backend, binary, request,
                session_id=result["creator_session_id"], resume=True, workspace=workspace,
                timeout=timeout_sec, log=directory / "repair.jsonl", model=model)
            if session != result["creator_session_id"]:
                raise RuntimeError("Repair returned a different Creator session.")
            if not handoff.is_file() or not handoff.read_text(encoding="utf-8").strip():
                raise RuntimeError(f"Creator produced no nonempty repair handoff: {handoff}")
            active["status"] = "auditing"
            result["stage"] = f"round_{number}_audit"
            save()
            reports = audit(directory / "audit")
            active["status"] = "completed"
            result["rounds_completed"] = number
            result["final_audit"] = active["audit_manifest"]
            save()
            active = None
        result.update(status="completed", stage="completed")
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        result.update(status="failed", error=str(error))
        if active is not None:
            active.update(status="failed", error=str(error))
        progress(f"Pipeline FAILED at {result['stage']}: {error}")
    except BaseException:
        result["status"] = "interrupted"
        if active is not None:
            active["status"] = "interrupted"
        raise
    finally:
        save()
    return result


def add_arguments(parser):
    creator.add_arguments(parser)
    parser.add_argument("--rounds", type=int, default=3,
                        help="Repair/re-audit cycles after the initial audit (default: 3)")
    parser.add_argument("--memory-dir", type=Path, help="Override the four audit prompt files")
    # The existing create command only needs three prompts; the pipeline needs repair too.
    for action in parser._actions:
        if action.dest == "prompt_dir":
            action.help = "Directory with research/build/nudge/repair_prompt.md"


def from_args(args, progress=None):
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    handling = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, interrupt) if handling else None
    try:
        return run_pipeline(action_group=args.action_group, env_dir=args.env_dir,
            workspace=args.workspace, rounds=args.rounds, backend=args.backend, model=args.model,
            output=args.output, prompt_dir=args.prompt_dir, memory_dir=args.memory_dir,
            claude_bin=args.claude_bin, codex_bin=args.codex_bin,
            timeout_sec=args.timeout_sec, progress=progress)
    finally:
        if handling:
            signal.signal(signal.SIGTERM, previous)
