"""Executable agent fixtures: feedback, session identity and failure boundaries."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from gym_anything_robotics import cli
from gym_anything_robotics.creation_audit import creator, method, pipeline


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys, time, uuid
a = sys.argv[1:]
cc = '-p' in a
resume = '--resume' in a if cc else 'resume' in a
prompt = a[a.index('-p') + 1] if cc else a[a.index('--json') + 1]
session = (a[a.index('--resume' if resume else '--session-id') + 1] if cc else
           a[a.index('resume') + 1] if resume else str(uuid.uuid4()))
state_path = pathlib.Path('.fixture-' + session)
if resume and not state_path.exists():
    sys.exit(15)
state = json.loads(state_path.read_text()) if resume else {}
mode = os.environ.get('PIPELINE_BEHAVIOR', '')
version_file = pathlib.Path('env/version.txt')
version = int(version_file.read_text()) if version_file.exists() else 0
report = None
if prompt.startswith('# Repair round'):
    kind = 'repair'
    assert resume and state['actor'] == 'creator'
    number = int(re.search(r'Repair round (\d+)', prompt)[1])
    feedback = pathlib.Path(re.search(r'audit feedback at (.+)\nand', prompt)[1])
    text = feedback.read_text()
    for role in ('qualitative', 'evidence', 'infrastructure', 'hack'):
        assert f'{role}: checked version {number - 1}; needs fixes' in text
    assert f'checked version {number};' not in text
    report = pathlib.Path(re.search(r'^Save your repair handoff to: (.+)$', prompt, re.M)[1])
    if mode == 'repair-timeout':
        time.sleep(20)
    if mode == 'repair-exit':
        sys.exit(7)
    version_file.write_text(str(number))
elif 'Note: save file is ' in prompt:
    kind = 'audit'
    assert resume and state['actor'] == 'auditor'
    report = pathlib.Path(prompt.split('Note: save file is ', 1)[1].split('\n', 1)[0])
    role = report.parent.name
    if mode == f'audit-{version}-{role}':
        sys.exit(7)
elif prompt.startswith('deep explore'):
    kind = 'explore'
    assert not resume
    state = {'actor': 'auditor'}
else:
    match = re.search(r'^Save your (research|handoff) to: (.+)$', prompt, re.M)
    if match:
        report = pathlib.Path(match[2])
        kind = 'research' if match[1] == 'research' else 'build'
        manifest = json.loads((report.parent/'run.json').read_text())
        if kind == 'research':
            assert not resume
            state = {'actor':'creator', 'handoff':str(report.parent/'handoff.md')}
        else:
            assert resume and state['actor'] == 'creator'
            if mode != 'missing-environment':
                pathlib.Path(manifest['env_dir']).mkdir(exist_ok=True)
                version_file.write_text('0')
        if mode == 'creation-exit' and kind == 'research':
            sys.exit(7)
    else:
        kind = 'nudge'
        assert resume and state['actor'] == 'creator'
        report = pathlib.Path(state['handoff'])
if report and not (kind == 'repair' and mode == 'repair-missing'):
    body = (f'{report.parent.name}: checked version {version}; needs fixes\n' if kind == 'audit'
            else f'{kind}: fixture evidence only\n')
    empty = (kind == 'repair' and mode == 'repair-empty' or
             kind == 'audit' and mode == f'audit-empty-{version}-{report.parent.name}')
    report.write_text('' if empty else body)
state_path.write_text(json.dumps(state))
with open('calls.jsonl', 'a') as stream:
    stream.write(json.dumps(dict(kind=kind, session=session, resume=resume, prompt=prompt,
                                version=version, args=a))+'\n')
returned = str(uuid.uuid4()) if kind == 'repair' and mode == 'repair-wrong-session' else session
if cc:
    print(json.dumps(dict(session_id=returned, is_error=False, subtype='success')))
else:
    print(json.dumps({'type':'thread.started', 'thread_id':returned}))
    print(json.dumps({'type':'turn.completed'}))
'''


@unittest.skipUnless(os.name == "posix", "POSIX agent process runner")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pipeline fixture ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "group.md").write_text("Grounded fixture actions, no actual physics.")
        self.binary = self.root / "agent"
        self.binary.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.binary.chmod(0o700)

    def run_pipeline(self, **overrides):
        kwargs = dict(action_group="group.md", env_dir="env", workspace=self.root,
                      backend="codex", codex_bin=str(self.binary), claude_bin=str(self.binary),
                      progress=lambda _: None)
        kwargs.update(overrides)
        return pipeline.run_pipeline(**kwargs)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_three_full_cycles_both_backends(self):
        for backend in ("cc", "codex"):
            with self.subTest(backend=backend):
                result = self.run_pipeline(backend=backend)
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["rounds_completed"], 3)
                calls = self.calls()[-38:]
                self.assertEqual(len(calls), 38)  # 3 create + 8 baseline + 3 * (1 repair + 8 audit)
                creators = [c for c in calls if c['kind'] not in ('explore', 'audit')]
                self.assertEqual(len(creators), 6)
                self.assertEqual({c['session'] for c in creators}, {result['creator_session_id']})
                self.assertEqual([c['resume'] for c in creators], [False, True, True, True, True, True])
                explores = [c for c in calls if c['kind'] == 'explore']
                self.assertEqual(len({c['session'] for c in explores}), 16)
                self.assertNotIn(result['creator_session_id'], {c['session'] for c in explores})
                self.assertEqual([c['version'] for c in calls if c['kind'] == 'audit'],
                                 [0]*4 + [1]*4 + [2]*4 + [3]*4)
                for entry in result['rounds']:
                    self.assertEqual(entry['status'], 'completed')
                    for source in entry['sources']:
                        data = Path(source['report']).read_bytes()
                        self.assertEqual(source['sha256'], hashlib.sha256(data).hexdigest())
                        self.assertIn(data.decode(), Path(entry['feedback']).read_text())
                last = json.loads(Path(result['final_audit']).read_text())
                self.assertTrue(all('version 3; needs fixes' in Path(a['report']).read_text()
                                    for a in last['auditors'].values()))
                self.assertIn('not an environment pass', result['qualification'])
                self.assertEqual(json.loads((Path(result['output_directory'])/'run.json').read_text()), result)

    def test_failed_creation_or_initial_audit_never_repairs(self):
        for mode in ('creation-exit', 'missing-environment', 'audit-0-evidence', 'audit-empty-0-evidence'):
            with self.subTest(mode=mode), patch.dict(os.environ, PIPELINE_BEHAVIOR=mode):
                # Each mode must start from a fresh candidate directory.
                env = self.root / 'env'
                if env.exists():
                    (env/'version.txt').unlink()
                    env.rmdir()
                before = len(self.calls())
                result = self.run_pipeline()
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(result['rounds_completed'], 0)
                self.assertFalse(any(c['kind'] == 'repair' for c in self.calls()[before:]))

    def test_failed_repair_never_launches_its_reaudit(self):
        for mode in ('repair-missing', 'repair-empty', 'repair-exit', 'repair-wrong-session', 'repair-timeout'):
            with self.subTest(mode=mode), patch.dict(os.environ, PIPELINE_BEHAVIOR=mode):
                result = self.run_pipeline(timeout_sec=2)
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(result['stage'], 'round_1_repair')
                self.assertEqual(result['rounds_completed'], 0)
                self.assertFalse((Path(result['output_directory'])/'round-01/audit').exists())
                self.assertTrue(Path(result['rounds'][0]['feedback']).is_file())

    def test_reaudit_failure_stops_later_rounds(self):
        with patch.dict(os.environ, PIPELINE_BEHAVIOR='audit-2-hack'):
            result = self.run_pipeline()
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['stage'], 'round_2_audit')
        self.assertEqual(result['rounds_completed'], 1)
        self.assertEqual(len(result['rounds']), 2)
        self.assertFalse((Path(result['output_directory'])/'round-03').exists())

    def test_interrupt_preserves_round_and_manifest(self):
        original = method._invoke
        def invoke(*args, **kwargs):
            if args[2].startswith('# Repair round'):
                raise KeyboardInterrupt
            return original(*args, **kwargs)
        output = self.root/'interrupted'
        with patch.object(method, '_invoke', side_effect=invoke), self.assertRaises(KeyboardInterrupt):
            self.run_pipeline(output=output)
        saved = json.loads((output/'run.json').read_text())
        self.assertEqual(saved['status'], 'interrupted')
        self.assertEqual(saved['rounds'][0]['status'], 'interrupted')
        self.assertTrue((output/'round-01/feedback.md').is_file())

    def test_invalid_inputs_and_existing_outputs_fail_before_calls(self):
        for kwargs in ({'rounds':0}, {'rounds':True}, {'rounds':1.5}, {'env_dir':self.root.parent},
                       {'action_group':'missing'}, {'prompt_dir':self.root}, {'output':self.root}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, FileExistsError)):
                self.run_pipeline(**kwargs)
        self.assertEqual(self.calls(), [])

    def test_custom_audit_prompts_are_also_shown_to_creator(self):
        memory = self.root/'custom audits'
        memory.mkdir()
        for role in method.AUDITORS:
            (memory/f'{role}_prompt.md').write_text(f'CUSTOM CRITERION: {role}')
        result = self.run_pipeline(rounds=1, memory_dir=memory)
        self.assertEqual(result['status'], 'completed', result)
        build = next(c for c in self.calls() if c['kind'] == 'build')
        repair = next(c for c in self.calls() if c['kind'] == 'repair')
        self.assertIn(str(memory), build['prompt'])
        self.assertIn(str(memory), repair['prompt'])
        copied = Path(result['output_directory'])/'round-01/audit/hack/prompt.md'
        self.assertEqual(copied.read_text(), 'CUSTOM CRITERION: hack')

    def test_cli_command_and_failure_exit(self):
        args = ['create-audit', '--workspace', str(self.root), '--action-group', 'group.md',
                '--env-dir', 'env', '--backend', 'codex', '--codex-bin', str(self.binary), '--rounds', '1']
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        self.assertIn('Repair/re-audit cycles: 1 / 1', out.getvalue())
        self.assertIn('not an environment pass', out.getvalue())
        with patch.dict(os.environ, PIPELINE_BEHAVIOR='audit-0-evidence'), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 1)

    def test_fixed_checks_run_once_per_batch_and_failures_reach_creator(self):
        observations = []
        def fixed_checks(*, plan, output, workspace, progress):
            output.mkdir()
            result = dict(schema='gar.check-results.v1', status='failed',
                          checks=[dict(name='performance', status='failed')])
            (output/'results.json').write_text(json.dumps(result))
            observations.append((len(self.calls()), output))
            return result
        with patch('gym_anything_robotics.checks.run_checks', side_effect=fixed_checks):
            result = self.run_pipeline(check_plan=self.root/'explicit-plan.json')
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual([n for n, _ in observations], [3, 12, 21, 30])
        for cycle in result['rounds']:
            source = next(s for s in cycle['sources'] if s['auditor'] == 'fixed_checks')
            self.assertEqual(source['sha256'], hashlib.sha256(Path(source['report']).read_bytes()).hexdigest())
            self.assertIn('"performance"', Path(cycle['feedback']).read_text())
        audits = [c for c in self.calls() if c['kind'] == 'audit']
        self.assertEqual(len(audits), 16)
        self.assertTrue(all('Fixed deterministic checks ran' in c['prompt'] for c in audits))

    def test_invalid_discovered_plan_fails_before_model_calls(self):
        env = self.root/'env'; env.mkdir()
        (env/'checks.json').write_text('{"schema":"invalid"}')
        result = method.run_audits(env_dir=env, workspace=self.root, backend='codex',
                                   codex_bin=str(self.binary), progress=lambda _: None)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('gar.check-plan.v1', result['error'])
        self.assertEqual(self.calls(), [])


class PromptReferenceTests(unittest.TestCase):
    def test_default_prompts_use_shipped_context_and_real_sources(self):
        self.assertTrue(creator.CONTEXT.is_file())
        workspace = Path(__file__).resolve().parents[1]
        guide = creator.CONTEXT.read_text()
        paths = re.findall(r'`((?:src/|tests/|benchmarks/)[^`]+)`', guide)
        self.assertGreater(len(paths), 10)
        for relative in paths:
            self.assertTrue((workspace/relative).exists(), relative)
        for name in ('research', 'build'):
            text = (creator.PROMPTS/f'{name}_prompt.md').read_text()
            self.assertIn('{context_file}', text)
            for stale in ('docs/lessons/', 'docs/unified-', 'robot-environment-quality-verbatim', 'robots/README.md'):
                self.assertNotIn(stale, text)


if __name__ == '__main__':
    unittest.main()
