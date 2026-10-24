"""Offline regression controls; fabricated native bytes are never executed."""
from __future__ import annotations
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'modules/common/runtime/codex_install.py'
TERRAFORM=shutil.which('terraform')
spec=importlib.util.spec_from_file_location('codex_installer_under_test',SOURCE)
codex=importlib.util.module_from_spec(spec);spec.loader.exec_module(codex)

class CodexInstallTest(unittest.TestCase):
    def setUp(self):
        storage=ROOT/'.tmp/agent-stack-tests';storage.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=storage);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.home=self.root/'codex-home';self.bin=self.root/'command-bin';self.urls=[];self.native_runs=[]
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('offline controls forbid network')))
        self.enterContext(patch.dict(os.environ,{'PATH':'/usr/bin:/bin','HOME':str(self.root)},clear=True))
    def package(self,version='0.145.0',system='Linux',machine='x86_64',legacy=False,extra=None):
        os_name,cpu,triple=codex.PLATFORMS[system,machine];native=version+'-'+os_name+'-'+cpu;prefix='package/vendor/'+triple+'/'
        files={prefix+('codex/codex'if legacy else'bin/codex'):b'FABRICATED NATIVE DATA - DO NOT EXECUTE',
               prefix+('path/rg'if legacy else'codex-path/rg'):b'FABRICATED HELPER',prefix+'codex-resources/nested/helper':b'KEEP ALL HELPER BYTES',
               'package/package.json':json.dumps({'name':'@openai/codex','version':native,'os':[os_name],'cpu':[cpu]}).encode()}
        if not legacy:files[prefix+'codex-package.json']=b'{"schema":1}'
        if extra:files.update(extra)
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w:gz')as t:
            for name,body in files.items():
                m=tarfile.TarInfo(name);m.size=len(body);m.mode=0o755 if name.endswith(('/codex','/rg','/helper'))else 0o644;t.addfile(m,io.BytesIO(body))
        archive=buf.getvalue();url=codex.REGISTRY+'-/codex-'+native+'.tgz'
        main={'name':'@openai/codex','version':version,'optionalDependencies':{'@openai/codex-'+os_name+'-'+cpu:'npm:@openai/codex@'+native}}
        meta={'name':'@openai/codex','version':native,'os':[os_name],'cpu':[cpu],'dist':{'tarball':url,'integrity':'sha512-'+base64.b64encode(hashlib.sha512(archive).digest()).decode()}}
        return main,meta,archive,files
    def fallback(self,version='0.145.0',release=None,system='Linux',machine='x86_64',code=404,legacy=False,alter=None):
        main,meta,archive,files=self.package(version,system,machine,legacy)
        if alter:main,meta,archive=alter(main,meta,archive)
        selector='latest'if release in(None,'stable','latest')else release
        def download(url,target,limit):
            self.urls.append(url)
            if url==codex.INSTALLER:raise urllib.error.HTTPError(url,code,'fixture missing',{},None)
            if url==codex.REGISTRY+selector:raw=json.dumps(main).encode()
            elif url==codex.REGISTRY+meta['version']:raw=json.dumps(meta).encode()
            elif url==meta['dist']['tarball']:raw=archive
            else:raise AssertionError('unexpected download '+url)
            self.assertLessEqual(len(raw),limit);target.write_bytes(raw)
        def version_of(path,home):
            self.assertTrue(path.is_file());self.native_runs.append(path);return version
        with patch.object(codex,'download',side_effect=download),patch.object(codex,'version_of',side_effect=version_of),patch.object(codex.platform,'system',return_value=system),patch.object(codex.platform,'machine',return_value=machine):
            codex.install(self.home,self.bin,release or 'stable')
        return files
    def assert_layout(self,version='0.145.0'):
        release=self.home/'packages/standalone/releases'/version;current=self.home/'packages/standalone/current'
        self.assertEqual(current.resolve(),release);self.assertEqual((current/'codex').resolve(),(release/'bin/codex').resolve())
        self.assertEqual((self.bin/'codex').resolve(),(release/'bin/codex').resolve())
        self.assertEqual((release/'codex-resources/nested/helper').read_bytes(),b'KEEP ALL HELPER BYTES')
        self.assertEqual((release/'codex-path/rg').read_bytes(),b'FABRICATED HELPER');self.assertEqual(len(self.native_runs),2)
    def test_CI01_1_standalone_success_no_npm(self):
        def download(url,target,limit):self.urls.append(url);self.assertEqual(url,codex.INSTALLER);target.write_bytes(b'FIXTURE INSTALLER DATA')
        def run(argv,**kw):
            self.assertEqual(argv[0],'sh');self.assertEqual(kw['env']['CODEX_RELEASE'],'0.145.0');self.assertEqual(kw['env']['CODEX_HOME'],str(self.home));return SimpleNamespace(returncode=0)
        with patch.object(codex,'download',side_effect=download),patch.object(codex.subprocess,'run',side_effect=run),patch.object(codex,'version_of',return_value='0.145.0'):
            codex.install(self.home,self.bin,'0.145.0')
        self.assertEqual(self.urls,[codex.INSTALLER])
    def standalone_http200(self, release, installed_version, installer_error=None):
        response=io.BytesIO(b'FABRICATED OFFICIAL INSTALLER - NEVER EXECUTED')
        response.status=200
        opener=SimpleNamespace(open=lambda request,timeout:response)
        def run(argv,**kw):
            self.assertEqual(argv[0],'sh')
            self.assertEqual(kw['env']['CODEX_RELEASE'],'latest'if release=='stable'else release)
            self.assertEqual(kw['env']['CODEX_HOME'],str(self.home))
            self.assertEqual(kw['env']['CODEX_INSTALL_DIR'],str(self.bin))
            self.assertEqual(kw['env']['CODEX_NON_INTERACTIVE'],'1')
            self.assertTrue(kw['check']);self.assertEqual(kw['timeout'],300)
            self.assertEqual(Path(argv[1]).read_bytes(),b'FABRICATED OFFICIAL INSTALLER - NEVER EXECUTED')
            if installer_error:raise installer_error
            return SimpleNamespace(returncode=0)
        with patch.object(codex.urllib.request,'build_opener',return_value=opener)as fetch,patch.object(codex.subprocess,'run',side_effect=run)as execution,patch.object(codex,'version_of',return_value=installed_version)as readiness,patch.object(codex,'install_native',side_effect=AssertionError('HTTP200 must never fall back to npm')):
            try:codex.install(self.home,self.bin,release)
            finally:
                self.assertEqual(fetch.call_count,1)
                self.assertEqual(execution.call_count,1)
        return readiness
    def test_CI01_4_http200_stable_latest_and_pin(self):
        for release in ('stable','latest','0.145.0'):
            with self.subTest(release=release):
                readiness=self.standalone_http200(release,'0.145.0')
                self.assertEqual(readiness.call_count,2)
                self.assertEqual(readiness.call_args_list[0].args,(self.home/'packages/standalone/current/bin/codex',self.home))
                self.assertEqual(readiness.call_args_list[1].args,(self.bin/'codex',self.home))
    def test_CI01_5_http200_retains_channel_and_pin_checks(self):
        for release,version in (('stable','0.145.0-alpha.18'),('latest','0.145.0-beta.1'),('0.145.0','0.146.0')):
            with self.subTest(release=release,version=version),self.assertRaises(ValueError):
                self.standalone_http200(release,version)
    def test_CI01_6_http200_installer_failure_never_npm(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.standalone_http200('stable','0.145.0',subprocess.CalledProcessError(1,['sh']))
    def test_CI01_2_missing_standalone_full_bundle(self):
        self.fallback();self.assert_layout();self.assertEqual(len(self.urls),4);self.assertIn(codex.REGISTRY+'latest',self.urls)
    def test_CI01_2_gone_standalone(self):
        self.fallback(code=410);self.assert_layout()
    def test_CI01_3_other_errors_never_fallback(self):
        for code in (401,403,429,500,503):
            with self.subTest(code=code),patch.object(codex,'download',side_effect=urllib.error.HTTPError(codex.INSTALLER,code,'fixture',{},None))as fetch,patch.object(codex,'install_native')as native:
                with self.assertRaises(urllib.error.HTTPError):codex.install(self.home,self.bin,'stable')
                native.assert_not_called();self.assertEqual(fetch.call_count,1)
        with patch.object(codex,'download',side_effect=urllib.error.URLError('fixture TLS/network error')),patch.object(codex,'install_native')as native:
            with self.assertRaises(urllib.error.URLError):codex.install(self.home,self.bin,'stable')
            native.assert_not_called()
    def test_CI01_3_installer_failure_never_fallback(self):
        with patch.object(codex,'download',side_effect=lambda u,p,l:p.write_bytes(b'DATA')),patch.object(codex.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['sh'])),patch.object(codex,'install_native')as native:
            with self.assertRaises(subprocess.CalledProcessError):codex.install(self.home,self.bin,'stable')
            native.assert_not_called()
    def test_CI02_1_exact_pin_and_prerelease(self):
        self.fallback(release='0.145.0');self.assert_layout();self.assertNotIn(codex.REGISTRY+'latest',self.urls)
        for version,release in [('0.145.0-alpha.18','0.145.0-alpha.18'),('0.145.0-beta.1','beta')]:
            self.assertEqual(codex.select_release(release,{'version':version}),version)
        for release,version in [('0.145.0','0.164.0'),('stable','0.145.0-alpha.18'),('alpha','0.145.0-beta.1')]:
            with self.assertRaises(ValueError):codex.select_release(release,{'version':version})
    def test_CI02_2_platforms(self):
        for system,machine in [('Linux','aarch64'),('Darwin','x86_64'),('Darwin','arm64')]:
            with self.subTest(platform=(system,machine)):
                self.home=self.root/(system+machine);self.bin=self.root/('bin-'+system+machine);self.fallback(system=system,machine=machine);self.assertTrue((self.bin/'codex').is_file())
        with patch.object(codex.platform,'system',return_value='Linux'),patch.object(codex.platform,'machine',return_value='riscv64'),patch.object(codex,'metadata')as meta:
            with self.assertRaises(ValueError):codex.install_native(self.home,self.bin,'stable',self.root)
            meta.assert_not_called()
    def test_CI03_1_corrupt_integrity_preserves_current(self):
        old=self.home/'packages/standalone/releases/old';old.mkdir(parents=True);current=old.parent.parent/'current';current.symlink_to(old)
        def alter(main,meta,archive):return main,meta,archive+b'tamper'
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):self.fallback(alter=alter)
        self.assertEqual(current.resolve(),old);self.assertFalse((self.bin/'codex').exists());self.assertEqual(self.native_runs,[])
    def test_CI03_1_wrong_metadata(self):
        for field in ('alias','URL','version','os','cpu','name'):
            with self.subTest(field=field):
                self.home=self.root/field;self.bin=self.root/('bin-'+field)
                def alter(main,meta,archive):
                    if field=='alias':main['optionalDependencies'].clear()
                    elif field=='URL':meta['dist']['tarball']='https://untrusted.invalid/native.tgz'
                    elif field=='version':main['version']='0.146.0'
                    else:meta[field]=['wrong']if field in('os','cpu')else'other'
                    return main,meta,archive
                with self.assertRaises((ValueError,AssertionError)):self.fallback(release='0.145.0',alter=alter)
                self.assertFalse((self.home/'packages/standalone/current').exists());self.assertEqual(self.native_runs,[])
    def test_CI03_2_unsafe_archives(self):
        for name,kind in [('package/../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('package/link',tarfile.SYMTYPE),('package/device',tarfile.CHRTYPE)]:
            buf=io.BytesIO()
            with tarfile.open(fileobj=buf,mode='w:gz')as t:
                m=tarfile.TarInfo(name);m.type=kind;m.linkname='/escape';t.addfile(m)
            with tarfile.open(fileobj=io.BytesIO(buf.getvalue()),mode='r:gz')as t:
                with self.assertRaises(ValueError):codex.archive_members(t,'x86_64-unknown-linux-musl')
    def test_CI03_2_wrong_platform_or_missing_binary(self):
        for extra in ({'package/vendor/wrong/bin/codex':b'wrong'},{'package/vendor/x86_64-unknown-linux-musl/bin/codex':b''}):
            main,meta,archive,files=self.package(extra=extra)
            p=self.root/'bundle.tgz';p.write_bytes(archive);target=self.root/'extracted';target.mkdir(exist_ok=True)
            if 'wrong'in next(iter(extra)):
                with self.assertRaises(ValueError):codex.unpack_bundle(p,target,meta['version'],'linux','x64','x86_64-unknown-linux-musl')
            else:
                # Missing binary, not merely a valid executable's version, is rejected.
                data=io.BytesIO()
                with tarfile.open(fileobj=data,mode='w:gz')as t:
                    m=tarfile.TarInfo('package/package.json');body=json.dumps({'name':'@openai/codex','version':meta['version'],'os':['linux'],'cpu':['x64']}).encode();m.size=len(body);t.addfile(m,io.BytesIO(body))
                p.write_bytes(data.getvalue())
                with self.assertRaises(ValueError):codex.unpack_bundle(p,target,meta['version'],'linux','x64','x86_64-unknown-linux-musl')
    def test_CI03_3_legacy_bundle_helpers_retained(self):
        self.fallback(legacy=True);self.assert_layout()
    def test_CI03_3_version_or_path_failure_preserves_current(self):
        main,meta,archive,files=self.package()
        with patch.object(codex,'version_of',return_value='wrong'):
            def data(url,target,limit):
                if url==codex.INSTALLER:raise urllib.error.HTTPError(url,404,'fixture',{},None)
                target.write_bytes(archive if url.endswith('.tgz')else json.dumps(meta if url.endswith(meta['version'])else main).encode())
            with patch.object(codex,'download',side_effect=data),patch.object(codex.platform,'system',return_value='Linux'),patch.object(codex.platform,'machine',return_value='x86_64'):
                with self.assertRaises(ValueError):codex.install(self.home,self.bin,'stable')
        self.assertFalse((self.home/'packages/standalone/current').exists())
        self.bin.mkdir();(self.bin/'codex').write_bytes(b'EXISTING COMMAND')
        with self.assertRaises(ValueError):self.fallback()
        self.assertEqual((self.bin/'codex').read_bytes(),b'EXISTING COMMAND');self.assertFalse((self.home/'packages/standalone/current').exists())
    def test_CI03_1_integrity_must_be_sha512(self):
        archive=self.root/'native.tgz';archive.write_bytes(b'fixture')
        for integrity in ('sha1-abcd','sha512-aW52YWxpZA==','sha512-***'):
            with self.assertRaises(ValueError):codex.verified_archive(archive,integrity)
    def test_CI03_2_duplicate_members(self):
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w:gz')as t:
            t.addfile(tarfile.TarInfo('package/duplicate'));t.addfile(tarfile.TarInfo('package/duplicate'))
        with tarfile.open(fileobj=io.BytesIO(buf.getvalue()),mode='r:gz')as t:
            with self.assertRaisesRegex(ValueError,'duplicate'):codex.archive_members(t,'x86_64-unknown-linux-musl')
    def test_CI03_3_restrictive_umask_keeps_bundle_readable(self):
        previous=os.umask(0o077)
        try:self.fallback();self.assert_layout()
        finally:os.umask(previous)
        release=self.home/'packages/standalone/current'
        for directory in [release.resolve(),*release.resolve().rglob('*')]:
            if directory.is_dir():self.assertEqual(directory.stat().st_mode&0o777,0o755)
    def test_CI03_1_redirects_cannot_downgrade_or_leave_npm_origin(self):
        handler=codex.HTTPSOnly();request=codex.urllib.request.Request(codex.REGISTRY+'latest')
        for destination in ('http://registry.npmjs.org/@openai/codex/latest','https://other.invalid/native'):
            with self.assertRaises(ValueError):handler.redirect_request(request,None,302,'fixture',{},destination)
    def test_CI04_1_host_function_uses_shared_installer(self):
        template=(ROOT/'modules/common/templates/runtime/install-agent-stack.sh.tpl').read_text()
        function=re.search(r'^install_host_codex_cli\(\) \{\n.*?^\}',template,re.M|re.S)[0]
        function=function.replace('${host_codex_cli_enabled}','true').replace('${admin_username}','admin').replace('/opt/agent-stack/codex-cli',str(self.home)).replace('/var/lib/apt/lists',str(self.root/'apt-lists'))
        tools=self.root/'tools';tools.mkdir();staging=self.root/'staging';staging.mkdir();(staging/'codex-install.py').write_bytes(SOURCE.read_bytes())
        scripts={'getent':'#!/bin/sh\necho "admin:x:1000:1000::'+str(self.root)+'/admin:/bin/bash"\n','id':'#!/bin/sh\necho fixture-group\n','apt-get':'#!/bin/sh\nexit 0\n',
                 'install':'#!/bin/bash\nmkdir -p "${@: -1}"\n','codex':'#!/bin/sh\necho "codex-cli 0.145.0"\n',
                 'python3':'#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME|$CODEX_INSTALL_DIR|${CODEX_RELEASE:-stable}|$1" >"$FIXTURE_CAPTURE"\n'}
        for name,body in scripts.items():p=tools/name;p.write_text(body);p.chmod(0o755)
        capture=self.root/'host-call.txt'
        script='set -euo pipefail\nstaging='+shlex.quote(str(staging))+'\ncommand() { if [ "$1" = -v ] && [ "$2" = codex ]; then return 1; fi; builtin command "$@"; }\n'+function+'\ninstall_host_codex_cli\n'
        for release in ('stable','0.145.0'):
            env={'PATH':str(tools)+':/usr/bin:/bin','FIXTURE_CAPTURE':str(capture),'CODEX_RELEASE':release}
            p=subprocess.run(['bash','-c',script],capture_output=True,env=env,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr.decode());self.assertEqual(capture.read_text().strip(),str(self.home)+'|/usr/local/bin|'+release+'|'+str(staging/'codex-install.py'))
        self.assertNotIn('curl -fsSL https://chatgpt.com/codex/install.sh',function)
    def test_CI04_2_workspace_exact_pin_and_staging(self):
        docker=(ROOT/'modules/common/templates/runtime/workspace.Dockerfile.tpl').read_text();main=(ROOT/'main.tf').read_text()
        self.assertIn('ARG CODEX_RELEASE=${workspace_codex_release}',docker);self.assertIn('CODEX_RELEASE="$CODEX_RELEASE"',docker)
        self.assertIn('COPY codex-install.py ',docker);self.assertIn('python3 /usr/local/libexec/agent-stack-codex-install.py',docker)
        self.assertIn('destination = "${local.runtime_staging_dir}/codex-install.py"',main)
        self.assertIn('file("${path.module}/modules/common/runtime/codex_install.py")',main)

    @unittest.skipUnless(TERRAFORM, 'local Terraform needed for provider-free fingerprint expression check')
    def test_CI04_3_installer_bytes_change_runtime_and_portable_identity(self):
        text=(ROOT/'main.tf').read_text()
        artifact=text.split('  runtime_artifact_checksum = ',1)[1].split('  runtime_staging_dir',1)[0].strip()
        self.assertIn('local.runtime_codex_installer,',artifact)
        # Keep the actual checksum expression. Fix unrelated deployment inputs so
        # only this helper can account for a changed hash.
        artifact=re.sub(r'local\.[A-Za-z0-9_.]+|var\.[A-Za-z0-9_.]+',
            lambda m:m[0] if m[0]=='local.runtime_codex_installer'else json.dumps('fixed:'+m[0]),artifact)
        source_map=text.split('  proof_runtime_sources = ',1)[1].split('  proof_runtime_checksum',1)[0].strip()
        proof=text.split('  proof_runtime_checksum = ',1)[1].split('  proof_provider_settings',1)[0].strip()
        self.assertIn('sources      = local.proof_runtime_sources',proof)
        self.assertIn('runtime  = local.proof_runtime_checksum',text)
        for key in ('settings','hermes_model','drive_skill','root_runtime'):
            proof=re.sub(r'(?m)^    '+key+r'\s*=.*$', '    '+key+' = "fixed unrelated input"',proof)
        config=self.root/'terraform-expression';config.mkdir()
        helper=config/'modules/common/runtime/codex_install.py';helper.parent.mkdir(parents=True);helper.write_bytes(SOURCE.read_bytes())
        (config/'main.tf').write_text('locals {\n runtime_codex_installer = file("${path.module}/modules/common/runtime/codex_install.py")\n runtime_artifact_checksum = '+artifact+'\n proof_runtime_sources = '+source_map+'\n proof_runtime_checksum = '+proof+'\n}\n')
        cli=config/'offline.tfrc';cli.write_text('disable_checkpoint = true\n')
        env={'PATH':'/usr/bin:/bin','CHECKPOINT_DISABLE':'1','TF_CLI_CONFIG_FILE':str(cli),'TF_DATA_DIR':str(config/'data'),'HOME':str(config)}
        def evaluate():
            p=subprocess.run([TERRAFORM,'-chdir='+str(config),'console','-no-color'],input='jsonencode({artifact=local.runtime_artifact_checksum, portable=local.proof_runtime_checksum})\n',text=True,capture_output=True,env=env,timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(json.loads(p.stdout))
        before=evaluate();helper.write_bytes(helper.read_bytes()+b'\n# fixture-only source mutation\n');after=evaluate()
        self.assertNotEqual(before['artifact'],after['artifact']);self.assertNotEqual(before['portable'],after['portable'])

if __name__=='__main__':unittest.main(verbosity=2)
