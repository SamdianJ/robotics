"""Human-first command presentations. Raw JSON is explicit, never a fallback."""
import json
import re
import shutil
import textwrap


def clean(value):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value))


def width():
    return max(40, min(110, shutil.get_terminal_size((100, 24)).columns))


def wrap(value, indent=""):
    return textwrap.fill(clean(value), width=width(), initial_indent=indent,
                         subsequent_indent=indent, break_long_words=False, break_on_hyphens=False)


def table(headers, rows):
    if not rows:
        return ""
    rows = [[clean(v if v is not None else "-") for v in row] for row in rows]
    columns = [min(max(len(str(h)), max(len(r[i]) for r in rows)), 28)
               for i, h in enumerate(headers)]
    # Preserve identifiers; let the final column wrap instead of spilling.
    columns[-1] = max(12, width() - sum(columns[:-1]) - 2 * (len(headers)-1))
    if sum(columns) + 2*(len(headers)-1) > width():
        return "\n\n".join("\n".join(wrap(f"{h}: {v}") for h, v in zip(headers, row))
                             for row in rows)
    lines = ["  ".join(str(h).ljust(columns[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        cells = [textwrap.wrap(v, columns[i], break_long_words=True, break_on_hyphens=False) or [""]
                 for i, v in enumerate(row)]
        for n in range(max(map(len, cells))):
            lines.append("  ".join((c[n] if n < len(c) else "").ljust(columns[i])
                                   for i, c in enumerate(cells)).rstrip())
    return "\n".join(lines)


def scalar(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, dict):
        return f"{len(value)} fields"
    if isinstance(value, list):
        if len(value) <= 7 and all(not isinstance(v, (dict, list)) for v in value):
            return "[" + ", ".join(scalar(v) for v in value) + "]"
        return f"{len(value)} items"
    return clean(value)


def fields(value, *, limit=12):
    rows = []
    for key, item in value.items():
        if key in ("schema", "schema_version", "metadata", "config", "source"):
            continue
        if isinstance(item, dict) and len(item) <= 6:
            for child, v in item.items():
                rows.append((f"{key}.{child}", scalar(v)))
        else:
            rows.append((key, scalar(item)))
    return table(("FIELD", "VALUE"), rows[:limit]), max(0, len(rows)-limit)


def joint_table(value):
    if not isinstance(value, dict) or not value or not all(isinstance(v, dict) and "position" in v for v in value.values()):
        return None
    return table(("JOINT", "POSITION", "TARGET", "VELOCITY"),
                 [(n, scalar(v["position"]), scalar(v.get("target")), scalar(v.get("velocity")))
                  for n, v in value.items()])


def _session(data, verbose=False, keys=False):
    state = "paused" if data.get("ready") and data.get("paused") else data.get("status", "unknown")
    identity = data.get("session_id", "")
    lines = [f"{data.get('environment', 'Session')}  {state.upper()}  {identity[:12]}"]
    task = data.get("task")
    if isinstance(task, dict):
        task = task.get("instruction") or task.get("description")
    if task:
        lines += [wrap(f"Task: {task}")]
    for key, title in (("url", "Server"), ("dashboard_url", "Dashboard"), ("help_url", "API help")):
        if data.get(key):
            lines += [f"{title}: {data[key]}"]
    if data.get("url"):
        lines += ["URLs are local to the server host; use your tunnel/preview remotely."]
    # A launch that failed or stopped has no server that accepts these keys: neither the heading nor the keys are
    # printed (`gar keys SESSION` still shows them).
    if keys and "api_keys" in data and data.get("status") not in ("failed", "stopped"):
        lines += ["", "API keys (secret):",
                  f"  Privileged: {data['api_keys'].get('privileged') or 'not enabled'}",
                  f"  Restricted: {data['api_keys'].get('policy') or 'not enabled'}"]
    for error in ("error", "close_error"):
        if data.get(error):
            lines += [wrap(f"Error: {data[error]}")]
    for warning in data.get("startup_warnings", []):
        if not verbose:
            warning = warning.split("Warning:", 1)[-1].strip()
            warning = warning.split("\n  newton.", 1)[0]
            warning = re.sub(r"'/(?:[^']+)'", "'<cache path>'", warning)
        lines += [wrap("Warning: " + warning)]
    if verbose:
        for key in ("session_id", "credential_file", "log", "listen_host"):
            if data.get(key):
                lines += [f"{key}: {data[key]}"]
        if data.get("log_tail"):
            lines += ["Recent log:", clean(data["log_tail"])]
    if identity:
        lines += ["", f"Next: gar dashboard {identity[:12]}  |  gar logs {identity[:12]}"] if data.get("ready") else [
            "", f"Inspect: gar logs {identity[:12]}  |  gar status {identity[:12]}"]
    return "\n".join(lines)


def render(kind, data, *, verbose=False):
    if kind == "environments":
        return "Available environments\n\n" + table(("ENVIRONMENT", "ROBOT", "ENGINE"),
            [(r["environment_id"], r["default_robot"], f"{r['backend']} {r['engine_version']}") for r in data]) + "\n\nNext: gar inspect ENV  |  gar tasks ENV  |  gar run ENV"
    if kind == "inspect":
        return "\n".join([f"{data['environment_id']}  |  {data['default_robot']}  |  {data['backend']} {data['engine_version']}",
                          wrap(data["notes"]), "",
                          "Robot choices: "+", ".join(x["id"] for x in data.get("robots",[])),
                          f"Tasks: gar tasks {data['environment_id']}",
                          f"Check setup: gar doctor {data['environment_id']}",
                          f"Launch options: gar run {data['environment_id']} --help"])
    if kind == "doctor":
        lines = [f"{data['environment']['environment_id']}  DEPENDENCIES {'OK' if data['ok'] else 'MISSING'}",
                 table(("PACKAGE", "VERSION"), list(data["coordinator_packages"].items()))]
        if data.get("environment_checks"):
            lines.append(table(("ENVIRONMENT CHECK", "OK", "DETAIL"),
                               [(c["check"], "yes" if c["ok"] else "NO", c["detail"]) for c in data["environment_checks"]["checks"]]))
        lines.append(wrap(data["scope"]))
        if verbose:
            lines += [f"Python: {data['python']}", "Not checked: " + ", ".join(data["not_checked"])]
        if not data["ok"]:
            lines += ["Select the configured robotics runtime, then rerun this check."]
        return "\n\n".join(lines)
    if kind == "catalogs":
        return "Task catalogues\n\n" + table(("ENVIRONMENT", "TASKS", "SELECTORS"),
            [(r["environment_id"], r["count"], ", ".join(r["selectors"]) or "fixed task") for r in data]) + "\n\nDescriptions and choices: gar tasks ENV"
    if kind == "catalog":
        lines = [data["environment_id"], wrap(data["description"])]
        choices = data.get("selector_choices", {})
        for key, values in choices.items():
            lines += [f"{key}: " + ", ".join(map(str, values))]
        lines += [f"Catalogue entries: {data['count']}",
                  "Selectors: " + (", ".join(data["selectors"]) or "none (one fixed task)")]
        if data["splits"]:
            lines += ["Splits: " + ", ".join(data["splits"])]
        if not data["evaluator"]["available"]:
            lines += ["Evaluator: unavailable through the shared API"]
        if data.get("cli_task_selector"):
            lines += [f"Start: gar run {data['environment_id']} --task NAME"]
        elif data["selectors"]:
            lines += [f"Resolve: gar tasks resolve {data['environment_id']} --select KEY=VALUE"]
        else:
            lines += [f"Start: gar run {data['environment_id']}"]
        if verbose:
            lines += [wrap(data["split_note"]), wrap(data["qualification"])]
        return "\n".join(lines)
    if kind == "task":
        lines = [data["task_id"], wrap(data["description"]),
                 "Selection: " + (", ".join(f"{k}={scalar(v)}" for k, v in data["selection"].items()) or "fixed task")]
        if data.get("saved_to"):
            lines += [f"Saved: {data['saved_to']}", f"Start: gar run --task-file {data['saved_to']}"]
        else:
            lines += ["Add --output task.json to save a reusable launch file."]
        return "\n".join(lines)
    if kind == "sessions":
        rows = data["sessions"]
        lines = ["All sessions" if data["all"] else "Active sessions"]
        if rows:
            lines += ["", table(("SESSION", "ENVIRONMENT", "STATUS", "TASK", "URL"),
                [(r["session_id"][:12], r["environment"], r["status"], r["task"], r.get("url") or "-") for r in rows]),
                "", "Use a unique environment name or the short ID above."]
        else:
            lines += ["No sessions." if data["all"] else "No active sessions.",
                      "Start one: gar run ENV  |  Browse: gar list"]
        if not data["all"]:
            lines += ["History: gar sessions --all"]
        lines += [wrap("Warning: " + v) for v in data.get("warnings", [])]
        if verbose:
            lines += [wrap(f"{r['session_id']}: {r.get('error', r['directory'])}") for r in rows]
        return "\n".join(lines)
    if kind in ("launch", "status"):
        return _session(data, verbose, keys=kind == "launch")
    if kind == "starting":
        return f"Starting session {data.get('session_id', '')[:12]} in background.\nCheck: gar status {data.get('session_id', '')[:12]}"
    if kind == "keys":
        return f"API keys for {data['session_id'][:12]} (secret)\nPrivileged: {data['privileged']}\nRestricted: {data.get('policy') or 'not enabled'}"
    if kind == "lifecycle":
        labels = {"pause": "Paused. Live state preserved.", "resume": "Resumed. Commands are allowed; no physics advanced.",
                  "stop": "Stopped. Recorded files are preserved." if data.get("stopped") else "Shutdown requested."}
        return f"{data['session_id'][:12]}  {labels[data['operation']]}"
    if kind == "logs":
        return f"Logs: {data['path']}\n\n" + ("\n".join(clean(v) for v in data["lines"]) or "(No log output yet.)")
    if kind == "dashboard":
        return f"Dashboard: {data['url']}\nSame live session; opening it does not reset the environment.\nEnter the owner or restricted key in the page. Use a tunnel/preview for remote access."
    if kind == "observation":
        lines = [f"Observation {data.get('observation_id', data.get('sequence', '-'))}  |  robot {data.get('robot', '-')}  |  time {scalar(data.get('sim_time_s'))} s"]
        images = data.get("images", {})
        lines += [table(("CAMERA", "IMAGE"), [(name, data.get("server_url", "") + info["url"] if info.get("url") else "unavailable") for name, info in images.items()])
                  if images else "No camera images in this observation."]
        proprioception = data.get("proprioception", {})
        joint_feedback = joint_table(proprioception.get("joints"))
        feedback, omitted = fields({k: v for k, v in proprioception.items() if k != "joints" or not joint_feedback},
                                   limit=30 if verbose else 8)
        if joint_feedback:
            lines += ["Joint feedback (native SI values):", joint_feedback]
        if feedback:
            lines += ["Robot feedback:", feedback]
        if omitted:
            lines += [f"{omitted} additional feedback fields; use --verbose or --format json."]
        return "\n\n".join(lines)
    if kind == "state":
        joints = joint_table(data)
        if joints:
            return "Joint state (native SI values)\n\n" + joints
        details, omitted = fields(data, limit=40 if verbose else 12)
        return "Privileged state\n\n" + details + "\n\nArrays and nested state are summarized. Use --format json for exact values." + (f" {omitted} more fields." if omitted else "")
    if kind == "actions":
        rows = []
        for name, info in data.get("action_types", {}).items():
            rows.append((name, ", ".join(map(str, info.get("required", []))) or "-"))
        return "Permitted actions (no action executed)\n\n" + table(("ACTION", "REQUIRED FIELDS"), rows) + "\n\nSend: gar action TARGET --file action.json\nFull parameter definitions: add --format json."
    if kind == "action":
        committed = data.get("committed")
        headline = "Action committed." if committed is True else "Action rejected; not executed." if committed is False else "Action outcome unknown."
        lines = [headline]
        if data.get("request_id"):
            lines += [f"Request: {data['request_id']}"]
        obs = data.get("observation") or {}
        if obs:
            lines += [f"Observation: {obs.get('observation_id', '-')}  |  time {scalar(obs.get('sim_time_s'))} s"]
        if data.get("observation_error") or committed is True and not obs:
            lines += ["Warning: command ran but observation is unavailable. Do not resend it."]
        if committed is None:
            lines += ["Do not retry blindly; reconcile the request with the owner."]
        if "reward" in data:
            lines += [f"Reward: {scalar(data['reward'])} (not a task-success verdict)"]
        if verbose and data.get("info"):
            lines += [fields(data["info"], limit=20)[0]]
        return "\n".join(lines)
    if kind == "evaluation":
        if not data["available"]:
            return "Task evaluator unavailable.\n" + wrap(data.get("reason", "No integrated evaluator."))
        text = f"Task {'SUCCEEDED' if data['success'] else 'NOT COMPLETE'}  |  score {scalar(data['score'])}"
        return text + ("\n\n" + fields(data.get("details", {}), limit=25)[0] if verbose else "")
    if kind == "benchmark":
        lines = [f"Benchmark {data['status'].upper()}",
                 f"Actions: {data.get('actions_completed', 0)} / {data.get('max_actions', '-')}",
                 f"Evidence: {data['output_directory']}"]
        if data.get("evaluation"):
            lines += [render("evaluation", data["evaluation"], verbose=verbose)]
        if data.get("error"):
            lines += [wrap("Error: " + str(data["error"]))]
        if data.get("wall_sim_ratio") is not None:
            lines += [f"Wall/simulation ratio: {scalar(data['wall_sim_ratio'])}"]
        return "\n".join(lines)
    if kind == "export":
        return "\n".join([f"Evidence saved: {data['output_directory']}",
            f"History records: {data['retained_records_exported']}  |  ticks {data['starts_at_tick']} to {data['ends_at_tick']}",
            f"Trace gaps: {len(data['trace_gaps'])}  |  discontinuities: {len(data['recorded_discontinuities'])}  |  incomplete records: {len(data['incomplete_records'])}",
            "This exports recorded history, not a video or proof of uninterrupted rollout."])
    if kind == "creator":
        lines = [f"Creator: {data['status']}", f"Environment source: {data['env_dir']}",
                 f"Session: {data.get('session_id') or '-'}", f"Logs: {data['output_directory']}"]
        for name, entry in data["turns"].items():
            lines.append(f"{name}: {entry['status']} — {entry['report']}")
        if data.get("error"):
            lines.append(f"Error: {data['error']}")
        if data["status"] == "turns_completed":
            lines.append("Creator turns completed; independent audits have not run.")
        return "\n".join(lines)
    if kind == "pipeline":
        lines = [f"Creation-audit pipeline: {data['status']}",
                 f"Stage: {data['stage']}", f"Environment source: {data['env_dir']}",
                 f"Repair/re-audit cycles: {data['rounds_completed']} / {data['rounds_requested']}",
                 f"Creator session: {data.get('creator_session_id') or '-'}",
                 f"Reports and logs: {data['output_directory']}"]
        if data.get("final_audit"):
            lines.append(f"Latest completed audit: {data['final_audit']}")
        if data.get("error"):
            lines.append(f"Error: {data['error']}")
        lines.append(data["qualification"])
        return "\n".join(lines)
    if kind == "audit":
        lines = [f"Audits: {data['status']}", f"Reports and logs: {data['output_directory']}"]
        if data.get("checks"):
            lines.append(f"Fixed checks: {data['checks']['status']}")
        for name, entry in data["auditors"].items():
            lines.append(f"{name}: {entry['status']} — {entry['report']}")
            if entry.get("error"):
                lines.append(f"  Error: {entry['error']}")
        lines.append("Report generation is not an environment pass; read the auditors' findings.")
        return "\n".join(lines)
    if kind == "checks":
        lines = [f"Fixed checks: {data['status']}", f"Results: {data['output_directory']}/results.json"]
        lines += [f"{item['status']}: {item['name']}" for item in data["checks"]]
        lines.append(data["qualification"])
        return "\n".join(lines)
    if kind == "test":
        return f"Contract tests {'PASSED' if data['ok'] else 'FAILED'}: {data['tests_run']} run, {data['failures']} failures, {data['errors']} errors.\nInterface checks only; not physical qualification."
    if kind == "robots":
        models=table(("MODEL","MOUNT","PROFILES"),[(x["id"],x["root"]["mounting"],", ".join(x.get("actuation",{}).get("profiles",{})) or "-") for x in data["models"]])
        setups=table(("PRESET","INSTANCES"),[(x["id"],", ".join(n+":"+v["model"] for n,v in x["instances"].items())) for x in data["setups"]])
        return "Shared robot catalog\n\n"+models+"\n\nDeployments\n\n"+setups+"\n\nUse gar robots inspect NAME; gar inspect ENV lists compatible selections."
    if kind == "robot":
        if "setup" in data:
            x=data["setup"]
            rows=[(n,v["model"],v["controller_profile"],v.get("mount","explicit pose")) for n,v in x["instances"].items()]
            return x["id"]+"\n\n"+table(("INSTANCE","MODEL","PROFILE","MOUNT"),rows)+"\n\n"+data["note"]+"\nUse --json for the complete resolved definition."
        x=data["model"]
        return x["id"]+"@"+str(x["revision"])+"\nMount: "+x["root"]["mounting"]+"\nProfiles: "+", ".join(x.get("actuation",{}).get("profiles",{}))+"\nAsset: "+x["asset"]["path"]+"\nUse --json for joints, frames, sources and mechanisms."
    if kind == "error":
        lines = [wrap("Error: " + data["error"])]
        if data.get("request_id"):
            lines += [f"Request: {data['request_id']}"]
        if "committed" in data:
            lines += [f"Committed: {scalar(data['committed'])}"]
        if data.get("hint"):
            lines += [wrap(data["hint"])]
        return "\n".join(lines)
    raise ValueError(f"No human presentation registered for {kind}")


def emit(kind, value, *, output_format="text", verbose=False, stream=None):
    import sys
    if output_format == "json":
        text = json.dumps(value, indent=2, allow_nan=False)
    else:
        text = render(kind, value, verbose=verbose)
    print(text, file=stream or sys.stdout, flush=True)
