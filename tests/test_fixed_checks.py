"""Fixed probes against the real HTTP/CLI stack and deliberately broken backends."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from gym_anything_robotics import checks, sessions
from gym_anything_robotics.action_history import ActionHistory
from gym_anything_robotics.env import RoboticsEnv
from gym_anything_robotics.policy_access import PolicyAccess
from gym_anything_robotics.remote import RemoteEnv
from gym_anything_robotics.session_service import SessionServer, SessionService
from tests.test_http_server import HTTPFakeBackend


def plan():
    return dict(schema=checks.SCHEMA,
                launch={"schema": "gar.launch.v1", "environment_id": "football"},
                cases=[dict(name="hold", action={"type": "cartesian_delta", "arm": "left",
                            "delta_m": [0, 0, 0]}, sim_s=1, repeat=2)], physics_trace=False)


class PlanTests(unittest.TestCase):
    def test_known_endpoint_regression_is_a_loadable_bounded_plan(self):
        _, data = checks.load_plan(Path(__file__).parent/'fixtures/checks/tabletop-push.json')
        endpoint = next(c for c in data['cases'] if c['name']=='joint4-upper-endpoint-regression')
        self.assertGreater(endpoint['sim_s']*endpoint['repeat'], 6.201)

    @unittest.skipUnless(os.name == 'posix', 'process group ownership')
    def test_partial_launch_failure_stops_only_its_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'plan.json').write_text(json.dumps(plan()))
            runner = checks.Checks(root/'plan.json', root/'output', root)
            owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     start_new_session=True, stderr=subprocess.DEVNULL)
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                         start_new_session=True, stderr=subprocess.DEVNULL)
            try:
                d = runner.output/'private/sessions/owned'; d.mkdir(parents=True)
                (d/'session.json').write_text(json.dumps(dict(status='starting', pid=owned.pid)))
                with patch.object(runner, 'launch', side_effect=RuntimeError('startup failed')):
                    result = runner.run()
                self.assertEqual(result['status'], 'failed')
                owned.wait(timeout=5)
                self.assertIsNone(unrelated.poll())
                self.assertTrue(any(c['name']=='cleanup.owned' and c['status']=='passed' for c in result['checks']))
            finally:
                for child in (owned, unrelated):
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)

    def test_verify_exit_uses_payload_including_unavailable(self):
        for available, success, code in ((True, True, 0), (True, False, 1), (False, None, 2)):
            value = dict(schema="gar.evaluation.v1", available=available, success=success)
            self.assertTrue(checks.verify_exit(code, value))
            self.assertFalse(checks.verify_exit((code+1) % 3, value))
        self.assertFalse(checks.verify_exit(1, {"error": "server failed"}))
        self.assertFalse(checks.verify_exit(1, dict(schema="gar.evaluation.v1", available=True, success="false")))

    def test_invalid_plan_rejected_before_session_or_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sessions, "start") as start:
            root = Path(tmp)
            for change in ({"cases": []}, {"max_wall_per_sim": float("nan")}, {"images": "false"},
                           {"schema": "v0"}, {"task_file": "also.json"}, {"typo": True}):
                data = {**plan(), **change}
                (root/"plan.json").write_text(json.dumps(data))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    checks.run_checks(plan=root/"plan.json", output=root/"results")
                self.assertFalse((root/"results").exists())
            start.assert_not_called()

    def test_interruption_during_offline_media_still_records_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'plan.json').write_text(json.dumps({**plan(), 'videos':[{'path':'video.mp4'}]}))
            runner = checks.Checks(root/'plan.json', root/'output', root)
            with patch.object(runner, 'launch', side_effect=RuntimeError('fixture startup failure')), \
                 patch.object(runner, 'cleanup') as cleanup, \
                 patch.object(checks, 'decode_video', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run()
                cleanup.assert_called_once()
            self.assertEqual(json.loads((root/'output/results.json').read_text())['status'], 'interrupted')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = HTTPFakeBackend(self.root)
        Image.new("RGB", (16, 9), "green").save(self.backend.camera_path)
        self.backend.policy_access = lambda: PolicyAccess(
            "yam", self.backend.capabilities.camera_specs, {("tick",): None},
            frozenset({"cartesian_delta"}), lambda: "Fixed check fixture", {})
        self.backend.act_policy = self.backend.act
        original_save = self.backend.save_checkpoint
        self.backend.save_checkpoint = lambda **kw: {**original_save(**kw), "durable": True}
        self.backend.evaluate = lambda: {"success": False, "score": 0.0}
        self.env = RoboticsEnv(self.backend)
        self.service = SessionService(self.env, session_id="probe-fixture", history=ActionHistory(self.root/"history.sqlite3"))
        self.server = SessionServer(("127.0.0.1", 0), self.service, api_token="owner-check-secret", policy_api_token="policy-check-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.session = self.root/"attached-fixture"
        self.session.mkdir()
        keys = self.root/"fixture-keys.json"
        keys.write_text(json.dumps(dict(privileged="owner-check-secret", policy="policy-check-secret")))
        (self.session/"session.json").write_text(json.dumps(dict(status="running", session_id="probe-fixture",
            url=self.url, launch={"credential_file": str(keys)})))

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.env.close()

    def run_probe(self, **changes):
        data = {**plan(), **changes}
        p = self.root/"plan.json"
        p.write_text(json.dumps(data))
        runner = checks.Checks(p, self.root/"results", Path(__file__).resolve().parents[1])
        def launch():
            runner.directory = self.session
            runner.tokens = ["owner-check-secret", "policy-check-secret"]
            runner.owner = RemoteEnv(self.url, api_key=runner.tokens[0])
            runner.policy = RemoteEnv(self.url, api_key=runner.tokens[1])
            runner.check("startup", True)
        with patch.object(runner, "launch", side_effect=launch):
            result = runner.run()
        return result

    def test_real_http_and_verify_cli_pass_incomplete_task(self):
        result = self.run_probe()
        self.assertEqual(result['status'], 'passed', result['checks'])
        rows = {c['name']: c for c in result['checks']}
        self.assertEqual(rows['cli.verify_contract']['details']['exit_code'], 1)
        self.assertEqual(rows['checkpoint.continuation']['status'], 'passed')
        self.assertEqual(rows['journal.actions_retained']['status'], 'passed')
        self.assertEqual(rows['journal.physics_complete']['status'], 'skipped')
        self.assertEqual(rows['videos']['status'], 'skipped')
        self.assertEqual(len(result['measurements']), 2)
        for file in (self.root/'results').glob('*.json'):
            self.assertNotIn('owner-check-secret', file.read_text())
            self.assertNotIn('policy-check-secret', file.read_text())

    def test_incorrect_duration_and_missing_trace_are_failures(self):
        value = plan()['cases'][0]
        value['sim_s'] = .01
        result = self.run_probe(cases=[value], physics_trace=True)
        self.assertEqual(result['status'], 'failed')
        failures = {c['name'] for c in result['checks'] if c['status'] == 'failed'}
        self.assertIn('action.hold.0', failures)
        self.assertIn('journal.physics_complete', failures)

    def test_corrupt_camera_cannot_pass(self):
        self.backend.camera_path.write_bytes(b'not an image')
        result = self.run_probe()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(c['name'] == 'interface' and c['status'] == 'failed' for c in result['checks']))

    def test_wrong_checkpoint_continuation_cannot_pass(self):
        original = self.backend.restore_checkpoint
        def broken(name):
            original(name)
            self.backend.tick += 1
            return self.backend._state(), {}
        self.backend.restore_checkpoint = broken
        result = self.run_probe()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(c['name'] == 'checkpoint.continuation' and c['status'] == 'failed' for c in result['checks']))

    def test_declared_checkpoint_branch_metadata_is_retained_but_not_compared(self):
        original_state = self.backend._state
        original_restore = self.backend.restore_checkpoint
        generation = [0]
        self.backend._state = lambda: {**original_state(), 'generation': generation[0]}
        def restore(name):
            original_restore(name)
            generation[0] += 1
            return self.backend._state(), {}
        self.backend.restore_checkpoint = restore
        result = self.run_probe(checkpoint_ignore_fields={'generation': 'Checkpoint branch counter'})
        self.assertEqual(result['status'], 'passed', result['checks'])
        saved = json.loads((self.root/'results/checkpoint.json').read_text())
        self.assertNotEqual(saved['first']['generation'], saved['replay']['generation'])
        self.assertEqual(saved['first']['tick'], saved['replay']['tick'])

    def test_checkpoint_exclusion_cannot_hide_wrong_physical_continuation(self):
        original_state = self.backend._state
        original_restore = self.backend.restore_checkpoint
        self.backend._state = lambda: {**original_state(), 'generation': 0}
        def restore(name):
            original_restore(name)
            self.backend.tick += 1
            return self.backend._state(), {}
        self.backend.restore_checkpoint = restore
        result = self.run_probe(checkpoint_ignore_fields={'generation': 'Branch metadata only'})
        self.assertTrue(any(c['name'] == 'checkpoint.continuation' and c['status'] == 'failed' for c in result['checks']))

    def test_speed_gate_is_not_masked_by_correct_function(self):
        result = self.run_probe(max_wall_per_sim=1e-9)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(all(c['status'] == 'failed' for c in result['checks'] if c['name'].endswith('.speed')))

    def test_interruption_retains_status_and_runs_cleanup(self):
        p = self.root/'plan.json'; p.write_text(json.dumps(plan()))
        runner = checks.Checks(p, self.root/'results', self.root)
        with patch.object(runner, 'launch', side_effect=KeyboardInterrupt), patch.object(runner, 'cleanup') as cleanup:
            with self.assertRaises(KeyboardInterrupt):
                runner.run()
            cleanup.assert_called_once()
        self.assertEqual(json.loads((self.root/'results/results.json').read_text())['status'], 'interrupted')


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required for actual decoder test')
class MediaTests(unittest.TestCase):
    def test_full_decode_frame_count_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp)/'short.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=16x16:r=5',
                            '-frames:v', '5', '-c:v', 'mpeg4', str(video)], check=True, timeout=20)
            passed, details = checks.decode_video(video, timeout=20, frames=5)
            self.assertTrue(passed, details)
            self.assertEqual(details['decoded_frames'], 5)
            self.assertFalse(checks.decode_video(video, timeout=20, frames=6)[0])
            video.write_bytes(video.read_bytes()[:40])
            self.assertFalse(checks.decode_video(video, timeout=20)[0])
