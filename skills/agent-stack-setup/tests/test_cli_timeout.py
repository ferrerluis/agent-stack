import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / 'modules/common/templates/runtime/install-agent-stack.sh.tpl'


class CLITimeoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = TEMPLATE.read_text(); start = text.index('run_openclaw_cli() {')
        function = text[start:text.index('\n}', start) + 2].replace('$${', '${')
        self.script = self.root / 'cli.sh'
        self.script.write_text('app="$CLI_FIXTURE_DIR"\n' + function + '\nrun_openclaw_cli models list --all --plain\n')
        self.program('docker', '''import json,os,signal,sys,time
from pathlib import Path
p=Path(os.environ['CLI_FIXTURE_DIR']); (p/'docker-args.json').write_text(json.dumps(sys.argv[1:]))
if os.environ.get('CLI_FIXTURE_MODE')=='host_hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN); (p/'pid').write_text(str(os.getpid()))
 while True:time.sleep(.1)
i=sys.argv.index('openclaw'); a=sys.argv[i+1:]; os.execvp(a[0],a)
''')
        self.program('openclaw', '''import json,os,signal,sys,time
from pathlib import Path
p=Path(os.environ['CLI_FIXTURE_DIR']); (p/'cli-args.json').write_text(json.dumps(sys.argv[1:]))
if os.environ.get('CLI_FIXTURE_MODE')=='child_hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN); (p/'pid').write_text(str(os.getpid()))
 while True:time.sleep(.1)
print('fixture-model')
''')

    def program(self, name, code):
        path = self.root / name
        path.write_text('#!/usr/bin/python3\n' + code); path.chmod(0o700)

    def run_cli(self, mode='', seconds='1'):
        env = dict(os.environ, CLI_FIXTURE_DIR=str(self.root), CLI_FIXTURE_MODE=mode,
                   OPENCLAW_CLI_TIMEOUT_SECONDS=seconds, PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        start = time.monotonic()
        child = subprocess.Popen(['bash', str(self.script)], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try: output, errors = child.communicate(timeout=22)
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL); child.communicate(); raise
        return child.returncode, output, errors, time.monotonic()-start

    def assert_terminated(self):
        pid = int((self.root/'pid').read_text()); status = Path('/proc')/str(pid)/'stat'
        if status.exists():
            text = status.read_text(); self.assertEqual(text[text.rfind(')')+2:].split()[0], 'Z')

    def test_noninteractive_command_preserves_arguments_and_returns_output(self):
        code, output, _, elapsed = self.run_cli()
        self.assertEqual(code, 0); self.assertIn(b'fixture-model', output); self.assertLess(elapsed, 3)
        self.assertIn('--interactive=false', json.loads((self.root/'docker-args.json').read_text()))
        self.assertEqual(json.loads((self.root/'cli-args.json').read_text()), ['models','list','--all','--plain'])

    def test_term_ignoring_command_is_killed_inside_the_container(self):
        code, _, _, elapsed = self.run_cli('child_hang')
        self.assertNotEqual(code, 0); self.assertLess(elapsed, 9); self.assert_terminated()

    def test_term_ignoring_docker_client_cannot_hold_setup_indefinitely(self):
        code, _, _, elapsed = self.run_cli('host_hang')
        self.assertNotEqual(code, 0); self.assertLess(elapsed, 19); self.assert_terminated()

    def test_invalid_timeout_never_invokes_docker(self):
        for seconds in ('0','1m','-1','$(false)'):
            code, _, _, _ = self.run_cli(seconds=seconds)
            self.assertEqual(code, 2); self.assertFalse((self.root/'docker-args.json').exists())
