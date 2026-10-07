import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[3]

class ImagePrefetchTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        text=(ROOT/'modules/common/templates/runtime/install-agent-stack.sh.tpl').read_text()
        start=text.index('prefetch_runtime_images() {');self.function=text[start:text.index('\n}',start)+2].replace('$${','${')
        fake=self.root/'docker'
        fake.write_text('''#!/usr/bin/python3
import json,os,signal,sys,time
from pathlib import Path
p=Path(os.environ['PREFETCH_FIXTURE']);a=sys.argv[1:]
with (p/'calls').open('a') as f:f.write(json.dumps(a)+'\\n')
mode=os.environ.get('PREFETCH_MODE','')
if 'config' in a:
 if mode=='bad_config':sys.exit(3)
 print('workspace' if mode=='workspace_only' else 'workspace\\nhermes\\nopenclaw')
elif 'pull' in a:
 if mode=='bad_pull':sys.exit(4)
 if mode=='hung_pull':
  signal.signal(signal.SIGTERM,signal.SIG_IGN);(p/'pid').write_text(str(os.getpid()))
  while True:time.sleep(.1)
''');fake.chmod(0o700)

    def run_prefetch(self,mode='',seconds='1'):
        env=dict(os.environ,PREFETCH_FIXTURE=str(self.root),PREFETCH_MODE=mode,PATH=str(self.root)+os.pathsep+os.environ['PATH'])
        script='set -euo pipefail\nstaging="$PREFETCH_FIXTURE/stage with spaces"\n'+self.function+'\nprefetch_runtime_images '+seconds+'\ntouch "$PREFETCH_FIXTURE/advanced"\n'
        began=time.monotonic();child=subprocess.Popen(['bash','-c',script],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:out,err=child.communicate(timeout=10)
        except BaseException:
            os.killpg(child.pid,signal.SIGKILL);child.communicate();raise
        calls=[json.loads(line) for line in (self.root/'calls').read_text().splitlines()] if (self.root/'calls').exists() else []
        return child.returncode,calls,time.monotonic()-began

    def test_only_remote_selected_services_are_pulled_and_stage_path_is_preserved(self):
        code,calls,_=self.run_prefetch();self.assertEqual(code,0)
        pulls=[a for a in calls if 'pull' in a];self.assertEqual(len(pulls),1)
        self.assertEqual(pulls[0][-2:],['hermes','openclaw']);self.assertNotIn('workspace',pulls[0])
        self.assertEqual(pulls[0][pulls[0].index('--env-file')+1],str(self.root/'stage with spaces/.env'))
        self.assertTrue((self.root/'advanced').exists())

    def test_workspace_only_never_attempts_a_registry_pull(self):
        code,calls,_=self.run_prefetch('workspace_only');self.assertEqual(code,0)
        self.assertFalse(any('pull' in a for a in calls))

    def test_failed_config_or_pull_stops_before_advancing(self):
        for mode in ('bad_config','bad_pull'):
            code,calls,_=self.run_prefetch(mode);self.assertNotEqual(code,0)
            self.assertFalse((self.root/'advanced').exists())
            if mode=='bad_config':self.assertFalse(any('pull' in a for a in calls))
            (self.root/'calls').unlink(missing_ok=True)

    def test_term_ignoring_pull_is_forcibly_bounded(self):
        code,_,elapsed=self.run_prefetch('hung_pull');self.assertNotEqual(code,0);self.assertLess(elapsed,9)
        self.assertFalse((self.root/'advanced').exists())
        proc=Path('/proc')/(self.root/'pid').read_text()/'stat'
        if proc.exists():
            text=proc.read_text();self.assertEqual(text[text.rfind(')')+2:].split()[0],'Z')

    def test_invalid_limit_never_calls_docker(self):
        code,calls,_=self.run_prefetch(seconds='0');self.assertEqual(code,2);self.assertEqual(calls,[])
