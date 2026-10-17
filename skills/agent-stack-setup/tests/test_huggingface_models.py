"""Offline HF acceptance: real setup/config helpers, shell bootstrap, no inference."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from test_render_tfvars import run_render

ROOT = Path(__file__).resolve().parents[3]
CATALOG = json.loads((ROOT / 'modules/common/runtime/huggingface-models.json').read_text())
REFS = ['huggingface/' + model['id'] for model in CATALOG]
spec = importlib.util.spec_from_file_location('hf_hermes_config', ROOT / 'modules/common/runtime/hermes_config.py')
hermes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermes)


def answers(**overrides):
    data = dict(cloud_provider='digitalocean', do_token='do-fixture', tailscale_enabled=False,
                model_providers_enabled=['huggingface'], huggingface_api_key='hf_OfflineFixtureOnly',
                default_model=REFS[0], fallback_models=REFS[1:], enabled_services=['openclaw', 'hermes'])
    data.update(overrides)
    return data


class HuggingFaceSetupTest(unittest.TestCase):
    def test_fresh_exports_preserve_nested_ids_and_defaults(self):
        for ref in REFS:
            _, rendered = run_render(answers(default_model=ref))
            self.assertIn('default_model = ' + json.dumps(ref), rendered)
            self.assertIn('huggingface_api_key = "hf_OfflineFixtureOnly"', rendered)
            self.assertIn('workspace_harnesses = [\n  "codex",\n]', rendered)
            self.assertIn('openai_api_key = ""', rendered)
            self.assertIn('groq_api_key = ""', rendered)

    def test_rejects_missing_and_unsafe_tokens_without_echoing_them(self):
        for key in ('', ' ', 'hf_...', 'YOUR_HF_TOKEN', "hf_Secret'Value", 'hf_Secret\nINJECTED=1', 'hf_Secret$HOME'):
            result, _ = run_render(answers(huggingface_api_key=key), expect_success=False)
            self.assertIn('huggingface_api_key', result.stderr)
            if key.strip() and len(key) > 10:
                self.assertNotIn(key, result.stderr)

    def test_requires_selected_provider_for_primary_fallback_and_override(self):
        cases = [dict(model_providers_enabled=['google'], gemini_api_key='fixture'),
                 dict(model_providers_enabled=['google'], gemini_api_key='fixture',
                      default_model='google/gemini-fixture', fallback_models=[], hermes_model=REFS[0])]
        for case in cases:
            result, _ = run_render(answers(**case), expect_success=False)
            self.assertIn('model_providers_enabled', result.stderr)

    def test_rejects_unknown_models_missing_org_and_unreviewed_provider_policies(self):
        for ref in ('huggingface/gpt-oss-20b', 'huggingface/org/arbitrary-weights:groq',
                    'huggingface/openai/gpt-oss-20b', 'huggingface/openai/gpt-oss-20b:cheapest',
                    'huggingface/openai/gpt-oss-20b:fastest', 'huggingface/openai/gpt-oss-20b:novita', 'huggingface/openai/gpt-oss-20b:groq',
                    'huggingface/openai/gpt-oss-120b:groq', 'huggingface/deepseek-ai/DeepSeek-V4.1-Flash-1017:baseten'):
            for field in ('default_model', 'fallback_models', 'hermes_model'):
                with self.subTest(ref=ref, field=field):
                    result, _ = run_render(answers(**{field: [ref] if field == 'fallback_models' else ref}), expect_success=False)
                    self.assertIn('reviewed Hugging Face route', result.stderr)

    def test_unused_hermes_override_does_not_activate_hf(self):
        for overrides in (dict(enabled_services=['openclaw']), dict(hermes_config_mode='preserve')):
            _, rendered = run_render(answers(model_providers_enabled=['google'], gemini_api_key='fixture',
                huggingface_api_key='', default_model='google/gemini-fixture', fallback_models=[],
                hermes_model='huggingface/org/unused:provider', **overrides))
            self.assertIn('huggingface_api_key = ""', rendered)


class HuggingFaceHermesTest(unittest.TestCase):
    def test_fresh_primary_and_ordered_fallbacks_use_scoped_chat_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes.seed_config(directory, dict(model_ref=REFS[0], fallback_models=[REFS[1], 'google/gemini-fixture']))
            path = Path(directory) / 'config.yaml'
            config = json.loads(path.read_text())
            self.assertEqual(config['model'], dict(provider='custom:agentstack-huggingface', default=CATALOG[0]['id']))
            self.assertEqual(config['fallback_providers'], [dict(provider='custom:agentstack-huggingface', model=CATALOG[1]['id']),
                                                          dict(provider='gemini', model='gemini-fixture')])
            provider = config['providers']['agentstack-huggingface']
            self.assertEqual(provider['key_env'], 'HF_TOKEN')
            self.assertEqual(provider['base_url'], 'https://router.huggingface.co/v1')
            self.assertEqual(provider['extra_body'], dict(parallel_tool_calls=False, reasoning_effort='high'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('hf_Offline', path.read_text())

    def test_manage_merges_hf_without_replacing_other_providers_or_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text('channels: {telegram: true}\nproviders:\n  private: {base_url: https://operator.example/v1}\nmodel: {default: old}\n')
            hermes.seed_config(directory, dict(model_ref='google/gemini-fixture', fallback_models=[REFS[0]]), 'manage')
            config = json.loads(path.read_text())
            self.assertEqual(config['channels'], dict(telegram=True))
            self.assertEqual(config['providers']['private']['base_url'], 'https://operator.example/v1')
            self.assertEqual(config['providers']['agentstack-huggingface']['extra_body']['parallel_tool_calls'], False)

    def test_auto_and_preserve_leave_existing_config_byte_identical(self):
        for mode in ('auto', 'preserve'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.yaml'
                original = 'model:\n  default: operator-model\n'
                path.write_text(original)
                self.assertFalse(hermes.seed_config(directory, dict(model_ref=REFS[0]), mode))
                self.assertEqual(path.read_text(), original)

    def test_non_hf_config_does_not_gain_provider_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes.seed_config(directory, dict(model_ref='google/gemini-fixture', fallback_models=[]))
            self.assertNotIn('providers', json.loads((Path(directory) / 'config.yaml').read_text()))


class HuggingFaceOpenClawTest(unittest.TestCase):
    def function(self, name):
        template = (ROOT / 'modules/common/templates/runtime/install-agent-stack.sh.tpl').read_text()
        return re.search(r'^' + name + r'\(\) \{\n.*?^\}', template, re.M | re.S)[0].replace('$${', '${')

    def fixture(self, command, config=None, selected=True, key=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'openclaw.json'
            path.write_text(json.dumps(config or {'channels': {'fixture': True}, 'models': {'providers': {'private': {'baseUrl': 'https://operator.example/v1'}}}}))
            script = 'set -euo pipefail\nOPENCLAW_CONFIG=' + shlex.quote(str(path)) + '\n'
            script += 'HUGGINGFACE_MODELS_JSON=' + shlex.quote(json.dumps(CATALOG)) + '\n'
            script += 'DEFAULT_MODEL_REF=' + shlex.quote(REFS[0]) + '\n'
            script += 'FALLBACK_MODELS_JSON=' + shlex.quote(json.dumps(REFS[1:])) + '\n'
            script += 'provider_selected() { ' + ('return 0' if selected else 'return 1') + '; }\n'
            script += 'has_env_key() { ' + ('return 0' if key else 'return 1') + '; }\n'
            script += 'mark_openclaw_restart_needed() { :; }\nfail() { echo "$*" >&2; exit 1; }\n'
            script += self.function('configure_huggingface_provider') + '\n'
            script += self.function('verify_huggingface_model_config') + '\n' + command
            result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, timeout=10)
            return result, json.loads(path.read_text()), path.stat().st_mode & 0o777

    def test_catalog_auth_and_sequential_tools_are_registered_idempotently(self):
        result, config, mode = self.fixture('configure_huggingface_provider\nconfigure_huggingface_provider')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config['channels'], dict(fixture=True))
        self.assertEqual(config['models']['providers']['private']['baseUrl'], 'https://operator.example/v1')
        provider = config['models']['providers']['huggingface']
        self.assertEqual(provider['api'], 'openai-completions')
        self.assertEqual(provider['apiKey'], dict(source='env', provider='default', id='HF_TOKEN'))
        self.assertEqual(provider['models'], CATALOG)
        self.assertEqual(mode, 0o600)
        for ref in REFS:
            self.assertIs(config['agents']['defaults']['models'][ref]['params']['parallel_tool_calls'], False)
            self.assertEqual(config['agents']['defaults']['models'][ref]['params']['thinking'], 'high')
        self.assertNotIn('hf_Offline', json.dumps(config))

    def test_missing_token_fails_closed_and_unselected_does_nothing(self):
        result, config, _ = self.fixture('configure_huggingface_provider', key=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Inference Providers permission', result.stderr)
        self.assertNotIn('huggingface', config['models']['providers'])
        result, config, _ = self.fixture('configure_huggingface_provider', selected=False, key=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('huggingface', config['models']['providers'])

    def test_exact_model_readback_detects_provider_suffix_loss_and_fallback_drift(self):
        good = dict(agents=dict(defaults=dict(model=dict(primary=REFS[0], fallbacks=REFS[1:]))))
        result, _, _ = self.fixture('verify_huggingface_model_config', config=good)
        self.assertEqual(result.returncode, 0, result.stderr)
        for primary, fallbacks in ((REFS[0].split(':')[0], [REFS[1]]), (REFS[0], []), ('openai/gpt-oss-20b', [REFS[1]])):
            bad = dict(agents=dict(defaults=dict(model=dict(primary=primary, fallbacks=fallbacks))))
            result, _, _ = self.fixture('verify_huggingface_model_config', config=bad)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('readback differs', result.stderr)

    def test_hf_fallback_readback_runs_even_when_another_primary_is_unusable(self):
        command = """
OPENCLAW_ENABLED=true
OPENCLAW_CONFIG_MODE_EFFECTIVE=manage
DEFAULT_MODEL_REF=google/unavailable-fixture
AGENT_CHANNEL=none
config_customizations_enabled() { return 0; }
provider_selected() { [ "$1" = huggingface ]; }
wait_openclaw_healthy() { return 0; }
configure_meta_provider() { :; }
run_openclaw_cli() { :; }
model_is_usable() { return 1; }
""" + self.function('configure_openclaw_channels_and_models') + "\nconfigure_openclaw_channels_and_models\n"
        result, _, _ = self.fixture(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readback differs', result.stderr)

    def test_runtime_credential_presence_uses_hf_token(self):
        function = self.function('model_route_has_credentials')
        script = 'has_env_key() { [ "$1" = HF_TOKEN ]; }\n' + function + '\nmodel_route_has_credentials huggingface\n'
        result = subprocess.run(['bash', '-c', script], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')

    def test_reviewed_reasoning_and_tool_contract_is_explicit(self):
        self.assertEqual([model['id'] for model in CATALOG], [
            'moonshotai/Kimi-K3:baseten', 'meta-models/Muse-Spark-Open-816B-A42B:baseten',
            'deepseek-ai/DeepSeek-V4.1-Flash:baseten'])
        expected_efforts = [['low', 'high', 'max'], ['low', 'medium', 'high', 'xhigh'], ['high', 'max']]
        for model, efforts in zip(CATALOG, expected_efforts):
            self.assertEqual(model['compat']['supportedReasoningEfforts'], efforts)
            self.assertIsNone(model['thinkingLevelMap']['off'])
            self.assertTrue(model['compat']['supportsTools'])
            self.assertTrue(model['compat']['requiresReasoningContentOnAssistantMessages'])
            self.assertEqual(model['contextWindow'], 1048576)
            self.assertEqual(model['input'], ['text'])

    def test_catalog_excludes_disabled_or_unsupported_thinking_choices(self):
        levels = {'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}
        for model in CATALOG:
            allowed = set(model['compat']['supportedReasoningEfforts'])
            self.assertEqual(set(model['thinkingLevelMap']), levels - allowed)
            self.assertTrue(all(value is None for value in model['thinkingLevelMap'].values()))
