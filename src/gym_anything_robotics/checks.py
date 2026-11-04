"""Repeatable, bounded checks of a newly owned GAR session, without a model call.

Plans contain data, not Python hooks. Results distinguish failed checks from
unconfigured coverage; successful execution is not physical qualification.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys
import time
from urllib.parse import quote
from uuid import uuid4

from . import cli_sessions, session_handoff, sessions
from .remote import RemoteEnv, RemoteError

SCHEMA = "gar.check-plan.v1"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def load_plan(path):
    path = Path(path).resolve(strict=True)
    data = json.loads(path.read_text())
    allowed = {"schema", "launch", "task_file", "cases", "videos", "source_files",
               "max_wall_per_sim", "startup_timeout_s", "request_timeout_s",
               "checkpoint", "checkpoint_ignore_fields", "physics_trace", "images"}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or set(data) - allowed:
        raise ValueError(f"Expected {SCHEMA}; unknown fields: {set(data) - allowed if isinstance(data, dict) else 'not an object'}")
    if ("launch" in data) == ("task_file" in data):
        raise ValueError("Supply exactly one of launch or task_file")
    json.dumps(data, allow_nan=False)
    cases = data.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 32:
        raise ValueError("Supply 1..32 bounded action cases")
    names = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"name", "action", "sim_s", "repeat"}:
            raise ValueError("A case requires name, action, sim_s and optional repeat")
        name = case.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Case names must be unique nonempty strings")
        names.add(name)
        if not isinstance(case.get("action"), dict) or not isinstance(case["action"].get("type"), str):
            raise ValueError("Each case needs a wire-format action object")
        positive(case.get("sim_s"), "case.sim_s")
        repeat = case.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 100:
            raise ValueError("case.repeat must be an integer in 1..100")
    for name in ("startup_timeout_s", "request_timeout_s", "max_wall_per_sim"):
        if name in data:
            positive(data[name], name)
    for name in ("checkpoint", "physics_trace", "images"):
        if name in data and type(data[name]) is not bool:
            raise ValueError(f"{name} must be boolean")
    excluded = data.get("checkpoint_ignore_fields", {})
    if not isinstance(excluded, dict) or any(not isinstance(k, str) or not k.strip()
            or not isinstance(v, str) or not v.strip() for k, v in excluded.items()):
        raise ValueError("checkpoint_ignore_fields maps top-level state fields to nonempty exclusion reasons")
    for name in ("source_files", "videos"):
        if not isinstance(data.get(name, []), list):
            raise ValueError(f"{name} must be a list")
    for video in data.get("videos", []):
        if not isinstance(video, dict) or set(video) - {"path", "frames", "sha256"} or not isinstance(video.get("path"), str):
            raise ValueError("A video requires path and optional frames/sha256")
        if "frames" in video and (type(video["frames"]) is not int or video["frames"] <= 0):
            raise ValueError("video.frames must be a positive integer")
        if "sha256" in video and (not isinstance(video["sha256"], str) or len(video["sha256"]) != 64):
            raise ValueError("video.sha256 must be a SHA-256 digest")
    for source in data.get("source_files", []):
        if not isinstance(source, str):
            raise ValueError("source_files entries must be paths")
    return path, data


def verify_exit(code, value):
    """An available but incomplete task returns 1, not an interface failure."""
    if not isinstance(value, dict) or value.get("schema") != "gar.evaluation.v1":
        return False
    if type(value.get("available")) is not bool:
        return False
    if value["available"] and type(value.get("success")) is not bool:
        return False
    if not value["available"] and value.get("success") is not None:
        return False
    expected = (0 if value["success"] else 1) if value["available"] else 2
    return code == expected


def decode_video(path, *, timeout, frames=None, sha256=None):
    """Decode the full stream; metadata inspection alone does not establish this."""
    path = Path(path).resolve(strict=True)
    command = ["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
               "-map", "0:v:0", "-an", "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    decoded = [int(line.split("=", 1)[1]) for line in result.stdout.splitlines() if line.startswith("frame=")]
    count = decoded[-1] if decoded else 0
    actual_hash = digest(path)
    passed = (result.returncode == 0 and count > 0 and "progress=end" in result.stdout
              and (frames is None or frames == count) and (sha256 is None or sha256 == actual_hash))
    return passed, dict(path=str(path), decoded_frames=count, expected_frames=frames,
                        sha256=actual_hash, expected_sha256=sha256,
                        exit_code=result.returncode, stderr=result.stderr)


class Checks:
    def __init__(self, plan, output, workspace, progress=None):
        self.path, self.plan = load_plan(plan)
        self.output = Path(output).resolve()
        self.workspace = Path(workspace).resolve(strict=True)
        self.output.mkdir(parents=True, exist_ok=False)
        self.progress = progress or (lambda _: None)
        self.result = dict(schema="gar.check-results.v1", status="running", started_at=time.time(),
                           plan_sha256=digest(self.path), plan_file=str(self.path),
                           output_directory=str(self.output), checks=[], measurements=[],
                           host={"platform": platform.platform(), "python": sys.version},
                           qualification="Fixed interface and evidence checks only; not physical qualification.")
        self.environment = dict(os.environ, GAR_SESSION_ROOT=str(self.output/"private/sessions"),
                                GAR_CREDENTIAL_ROOT=str(self.output/"private/credentials"),
                                PYTHONDONTWRITEBYTECODE="1")
        self.environment.pop("GAR_HTTP_API_TOKEN", None)
        self.environment.pop("GAR_HTTP_POLICY_API_TOKEN", None)
        (self.output/"private").mkdir(mode=0o700)
        self.timeout = self.plan.get("request_timeout_s", 120)
        self.directory = None
        self.owner = self.policy = None
        self.tokens = []
        self.action_ids = set()
        self.artifact_number = 0
        self.result["host"]["versions"] = {}
        for package in ("newton", "warp-lang", "mujoco", "pillow"):
            try:
                self.result["host"]["versions"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                self.result["host"]["versions"][package] = None
        if shutil.which("nvidia-smi"):
            try:
                gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,utilization.gpu",
                                      "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
                self.result["host"]["gpu_before"] = dict(exit_code=gpu.returncode, stdout=gpu.stdout, stderr=gpu.stderr)
            except (OSError, subprocess.SubprocessError) as error:
                self.result["host"]["gpu_before"] = dict(error=str(error))
        self.write("plan.json", self.plan)
        self.save()

    def write(self, name, data):
        # Published results never contain bearer keys, including exception text.
        text = json.dumps(data, indent=2, allow_nan=False)
        for token in self.tokens:
            if token:
                text = text.replace(token, "<redacted>")
        target = self.output/name
        temporary = target.with_suffix(target.suffix+".tmp")
        temporary.write_text(text+"\n")
        temporary.replace(target)

    def save(self):
        self.write("results.json", self.result)

    def check(self, name, passed, details=None):
        entry = dict(name=name, status="passed" if passed else "failed", details=details)
        self.result["checks"].append(entry)
        self.progress(f"{entry['status'].upper()}: {name}")
        self.save()
        return passed

    def skip(self, name, reason):
        self.result["checks"].append(dict(name=name, status="skipped", details=reason))
        self.save()

    def attempt(self, name, operation):
        try:
            return operation()
        except Exception as error:
            self.check(name, False, f"{type(error).__name__}: {error}")

    def cli(self, args, *, json_output=True):
        command = [sys.executable, "-m", "gym_anything_robotics.cli", *map(str, args)]
        if json_output:
            command += ["--format", "json"]
        p = subprocess.run(command, cwd=self.workspace, env=self.environment,
                           capture_output=True, text=True, timeout=self.timeout)
        self.artifact_number += 1
        self.write(f"cli-{self.artifact_number:03d}.json", dict(arguments=list(map(str, args)),
                   exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr))
        return p.returncode, json.loads(p.stdout) if json_output else p.stdout

    def launch(self):
        if "task_file" in self.plan:
            from .tasks import TaskSpec
            task = TaskSpec.load(self.path.parent/self.plan["task_file"])
            spec = task.launch
            self.write("task.json", task.to_dict())
            args = ["--task-file", str(self.output/"task.json")]
        else:
            from .launch import LaunchSpec
            spec = LaunchSpec.from_dict(self.plan["launch"])
            self.write("launch.json", spec.to_dict())
            args = ["--config", str(self.output/"launch.json")]
        if "output_dir" in spec.backend_options:
            raise ValueError("Check sessions own their output_dir; remove it from the launch")
        from .tasks import CATALOGS
        discovery = [["inspect", spec.environment_id], ["doctor", spec.environment_id]]
        if spec.environment_id in CATALOGS:
            discovery += [["tasks", "inspect", spec.environment_id], ["tasks", "list"]]
        else:
            self.skip("cli.tasks", "No task catalogue registered for this environment")
        for args_cli in discovery:
            for suffix, raw in (([], False), (["--verbose"], False), ([], True)):
                code, value = self.cli([*args_cli, *suffix], json_output=raw)
                self.check("cli."+".".join(args_cli)+( ".json" if raw else ".verbose" if suffix else ".text"),
                           code == 0 and bool(value), {"exit_code": code})
        start = time.perf_counter()
        code, receipt = self.cli(["run", *args, "--port", "0", "--policy-key", "--no-wait"])
        if code != 0:
            raise RuntimeError("Managed launch failed; see CLI capture")
        self.directory = Path(receipt["directory"])
        if not self.directory.is_relative_to(self.output/"private/sessions"):
            raise RuntimeError("Launch returned a session outside this run")
        ready = session_handoff.wait(self.directory, timeout_s=self.plan.get("startup_timeout_s", 180), pid=receipt["pid"])
        self.result["cold_start_wall_s"] = time.perf_counter()-start
        if not ready.get("ready"):
            raise RuntimeError("Session did not become ready; inspect private session log")
        keys = ready["api_keys"]
        self.tokens = list(keys.values())
        self.owner = RemoteEnv(ready["url"], api_key=keys["privileged"], timeout_s=self.timeout)
        self.policy = RemoteEnv(ready["url"], api_key=keys["policy"], timeout_s=self.timeout)
        self.result["session_id"] = receipt["session_id"]
        self.check("startup", True, {"cold_start_wall_s": self.result["cold_start_wall_s"]})

    def images(self, observation, *, persist=True):
        from PIL import Image
        items = observation.get("images", {})
        if self.plan.get("images", True) and not items:
            raise ValueError("No camera images in the policy observation")
        records = []
        for name, entry in items.items():
            data = self.policy.request(entry["url"], binary=True)
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                dimensions = list(image.size)
            expected = entry.get("camera", {}).get("resolution_px")
            if expected and dimensions != expected:
                raise ValueError(f"Camera {name}: decoded {dimensions}, advertised {expected}")
            self.artifact_number += 1
            filename = f"image-{self.artifact_number:03d}.png"
            record = dict(camera=name, dimensions=dimensions, bytes=len(data), artifact=filename)
            if persist:
                (self.output/filename).write_bytes(data)
            else:
                record['_content'] = data
            records.append(record)
        return records

    def act(self, case, *, key=None):
        before = self.policy.obs()["sim_time_s"]
        key = key or str(uuid4())
        start = time.perf_counter()
        value = self.policy.act(case["action"], idempotency_key=key)
        if value.get("ok") is not True or value.get("committed") is not True or not value.get("observation"):
            raise RuntimeError("Action did not commit with an observation; it will not be retried")
        images = self.images(value["observation"], persist=False)
        wall = time.perf_counter()-start
        # Saving the checker's own evidence is not part of simulator latency.
        for entry in images:
            (self.output/entry['artifact']).write_bytes(entry.pop('_content'))
        elapsed = value["observation"]["sim_time_s"]-before
        if not math.isclose(elapsed, case["sim_s"], rel_tol=1e-8, abs_tol=1e-10):
            raise ValueError(f"Action advanced {elapsed}s; expected {case['sim_s']}s")
        json.dumps(self.owner.get_state(), allow_nan=False)
        self.action_ids.add(value["request_id"])
        return value, dict(wall_s=wall, simulated_s=elapsed, wall_per_sim=wall/elapsed, images=images)

    def denied(self, path):
        try:
            self.policy.request(path)
        except RemoteError as error:
            self.check("policy.denied."+path, error.status == 403, {"http_status": error.status})
        else:
            self.check("policy.denied."+path, False, "Restricted key gained privileged access")

    def basics(self):
        before = self.owner.get_state()
        self.write("capabilities.json", self.owner.request("/v1/capabilities"))
        self.write("policy-capabilities.json", self.policy.request("/v1/capabilities"))
        decoded = self.images(self.policy.obs())
        if decoded:
            self.check("images.decode", True, decoded)
        else:
            self.skip("images.decode", "Imageless plan and observation; camera delivery not verified")
        self.owner.evaluate()
        self.check("reads.do_not_step", self.owner.get_state() == before)
        for path in ("/v1/state", "/v1/evaluation", "/v1/history", "/v1/geometry", "/v1/session"):
            self.denied(path)
        code, value = self.cli(["verify", str(self.directory)])
        self.check("cli.verify_contract", verify_exit(code, value), {"exit_code": code, "evaluation": value})
        before = self.owner.get_state()
        try:
            self.policy.act({"type": "__gar_invalid_action__"})
        except RemoteError as error:
            rejected = error.status in (400, 403)
        else:
            rejected = False
        self.check("invalid_action.nonmutation", rejected and self.owner.get_state() == before)
        case = self.plan["cases"][0]
        key = str(uuid4())
        first, _ = self.act(case, key=key)
        after = self.owner.get_state()
        second = self.policy.act(case["action"], idempotency_key=key)
        self.check("idempotency.once", first == second and self.owner.get_state() == after)
        self.owner.pause()
        try:
            self.policy.act(case["action"])
        except RemoteError as error:
            rejected = error.status == 400 and error.payload.get("committed") is False
        else:
            rejected = False
        finally:
            self.owner.resume()
        self.check("pause.nonmutation", rejected and self.owner.get_state() == after)

    def checkpoint(self):
        if not self.plan.get("checkpoint", True):
            self.skip("checkpoint", "Disabled explicitly by the plan; not verified")
            return
        saved = self.owner.get_state()
        excluded = self.plan.get("checkpoint_ignore_fields", {})
        def physical_state(state):
            if set(excluded) - state.keys() or not state.keys() - excluded.keys():
                raise ValueError("Checkpoint exclusions must name existing fields and leave state to compare")
            return {key: value for key, value in state.items() if key not in excluded}
        physical_state(saved)
        label = "fixed-check-"+uuid4().hex
        value = self.owner.request("/v1/checkpoints/save", payload={"label": label})
        self.check("checkpoint.durable", value["checkpoint"].get("durable") is True)
        self.act(self.plan["cases"][0])
        continuation = self.owner.get_state()
        self.owner.request("/v1/checkpoints/restore", payload={"checkpoint": label})
        restored = self.owner.get_state()
        self.check("checkpoint.restore", physical_state(restored) == physical_state(saved),
                   {"excluded_fields": excluded})
        self.act(self.plan["cases"][0])
        replay = self.owner.get_state()
        self.write("checkpoint.json", dict(saved=saved, restored=restored, first=continuation, replay=replay,
                                            excluded_fields=excluded))
        self.check("checkpoint.continuation", physical_state(continuation) == physical_state(replay),
                   {"excluded_fields": excluded})

    def cases(self):
        limit = self.plan.get("max_wall_per_sim", 200)
        for case in self.plan["cases"]:
            for repeat in range(case.get("repeat", 1)):
                name = f"action.{case['name']}.{repeat}"
                def execute():
                    _, sample = self.act(case)
                    sample.update(case=case["name"], repeat=repeat, max_wall_per_sim=limit)
                    self.result["measurements"].append(sample)
                    self.check(name, True, sample)
                    self.check(name+".speed", sample["wall_per_sim"] <= limit, sample)
                    return True
                if self.attempt(name, execute) is not True:
                    raise RuntimeError("Action check failed; remaining configured motions are not attempted")
                if not self.owner.request("/v1/health").get("ready"):
                    raise RuntimeError("Session faulted; later actions are not attempted")

    def journal(self):
        self.owner.pause()
        status = self.owner.request("/v1/history/status")["history"]
        cursor, records, traces, request_ids = 0, [], [], set()
        for _ in range(100):
            page = self.owner.request(f"/v1/history?after_seq={cursor}&limit=200")
            for row in page["records"]:
                record = self.owner.request("/v1/history/"+quote(row["id"], safe=""))["record"]
                records.append(record)
                if record["kind"] != "action":
                    continue
                request_ids.add(record.get("request_id"))
                if self.plan.get("physics_trace", True):
                    trace = self.owner.request("/v1/history/"+quote(row["id"], safe="")+"/trace?limit=1")
                    traces.append(dict(id=row["id"], available=trace.get("available"), summary=trace.get("summary")))
            if not page["has_more"]:
                break
            if page["next_after_seq"] <= cursor:
                raise RuntimeError("History pagination did not advance")
            cursor = page["next_after_seq"]
        else:
            raise RuntimeError("History exceeded the bounded 20,000-record check")
        self.write("journal.json", dict(status=status, records=records, traces=traces))
        self.check("journal.actions_retained", self.action_ids <= request_ids,
                   {"expected_actions": len(self.action_ids), "retained_actions": len(request_ids)})
        self.check("journal.healthy", status.get("durable") is True and not status.get("recording_fault")
                   and not status.get("evicted_records") and not status.get("incomplete_records"), status)
        if self.plan.get("physics_trace", True):
            self.check("journal.physics_complete", bool(traces) and all(
                t["available"] and t["summary"] and t["summary"]["complete"]
                and not t["summary"]["missing_tick_ranges"] and not t["summary"]["capture_errors"]
                for t in traces), {"traces": len(traces)})
        else:
            self.skip("journal.physics_complete", "Disabled explicitly; action history alone does not prove substep completeness")

    def cleanup(self):
        # The private session tree also covers an interrupted launch before a receipt.
        for manifest_path in (self.output/"private/sessions").glob("*/session.json"):
            directory = manifest_path.parent
            manifest = json.loads(manifest_path.read_text())
            pid = manifest.get("pid")
            if pid is None and (directory/"owner.lock").exists():
                pid = int((directory/"owner.lock").read_text())
            try:
                if manifest["status"] == "running":
                    try:
                        cli_sessions.stop(directory, timeout_s=min(self.timeout, 30))
                    except (OSError, ValueError, RuntimeError) as error:
                        self.check("cleanup.graceful_stop", False, str(error))
                if pid is None:
                    self.check("cleanup."+directory.name, False, "No child PID recorded; cleanup could not be verified")
                    continue
                if session_handoff._process_alive(pid):
                    # A new-session child owns this process group; never kill another group.
                    if os.getpgid(pid) != pid:
                        raise RuntimeError("Owned child no longer leads its process group")
                    os.killpg(pid, signal.SIGINT)
                    deadline = time.monotonic()+10
                    while session_handoff._process_alive(pid) and time.monotonic() < deadline:
                        time.sleep(.1)
                    if session_handoff._process_alive(pid):
                        from .creation_audit.method import _kill_process_group
                        _kill_process_group(pid)
                self.check("cleanup."+directory.name, not session_handoff._process_alive(pid))
            except (OSError, ValueError, RuntimeError) as error:
                self.check("cleanup."+directory.name, False, str(error))
        for client in (self.owner, self.policy):
            if client:
                client.close()

    def run(self):
        # Also cover interruptions/errors during cleanup or offline media checks.
        try:
            return self._run()
        except Exception as error:
            self.check("runner.finalize", False, f"{type(error).__name__}: {error}")
            self.result["status"] = "failed"
        except BaseException:
            self.result["status"] = "interrupted"
            raise
        finally:
            self.result["finished_at"] = time.time()
            self.save()
        return json.loads((self.output/"results.json").read_text())

    def _run(self):
        sources = [self.path, Path(__file__).resolve(), *(self.path.parent/p for p in self.plan.get("source_files", []))]
        try:
            self.result["source_hashes_before"] = {str(p.resolve()): digest(p) for p in sources}
            self.launch()
            self.attempt("interface", self.basics)
            self.attempt("checkpoint", self.checkpoint)
            self.attempt("actions", self.cases)
            self.attempt("journal", self.journal)
        except Exception as error:
            self.check("runner", False, f"{type(error).__name__}: {error}")
        except BaseException:
            self.result["status"] = "interrupted"
            raise
        finally:
            try:
                self.cleanup()
            finally:
                self.result["finished_at"] = time.time()
                self.save()
        for video in self.plan.get("videos", []):
            def inspect_video():
                passed, details = decode_video(self.path.parent/video["path"], timeout=self.timeout,
                                                frames=video.get("frames"), sha256=video.get("sha256"))
                self.check("video."+video["path"], passed, details)
            self.attempt("video."+video["path"], inspect_video)
        if not self.plan.get("videos"):
            self.skip("videos", "No video paths configured; no decoding or visual review claimed")
        def compare_sources():
            after = {str(p.resolve()): digest(p) for p in sources}
            self.result["source_hashes_after"] = after
            self.check("sources.unchanged", after == self.result.get("source_hashes_before"))
        self.attempt("sources.unchanged", compare_sources)
        self.result["status"] = "failed" if any(c["status"] == "failed" for c in self.result["checks"]) else "passed"
        self.result["finished_at"] = time.time()
        self.save()
        return json.loads((self.output/"results.json").read_text())


def run_checks(*, plan, output, workspace=None, progress=None):
    return Checks(plan, output, workspace or Path.cwd(), progress).run()


def add_arguments(parser):
    parser.add_argument("--plan", type=Path, required=True, help="Versioned JSON check plan")
    parser.add_argument("--output", type=Path, required=True, help="New results directory; never overwritten")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())


def from_args(args, progress=None):
    import threading
    handling = threading.current_thread() is threading.main_thread()
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    old = signal.signal(signal.SIGTERM, interrupt) if handling else None
    try:
        return run_checks(plan=args.plan, output=args.output, workspace=args.workspace, progress=progress)
    finally:
        if handling:
            signal.signal(signal.SIGTERM, old)
