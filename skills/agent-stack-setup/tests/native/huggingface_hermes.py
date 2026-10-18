"""Offline native Hermes request/history acceptance; supply a pinned source checkout.

Usage: python this-file.py /path/to/hermes-agent
The interpreter needs that checkout's pinned SDK/YAML/utility dependencies.
No agent, service, real credential or network connection is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[4]


def worker():
    def deny_network(event, args):
        if event == 'socket.connect':
            raise RuntimeError('Network prohibited by offline acceptance')
    sys.addaudithook(deny_network)
    import httpx
    from openai import OpenAI
    from agent.transports.chat_completions import ChatCompletionsTransport
    from agent.chat_completion_helpers import build_assistant_message
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider, _custom_provider_request_overrides
    spec = importlib.util.spec_from_file_location('agentstack_seed', ROOT / 'modules/common/runtime/hermes_config.py')
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    catalog = json.loads((ROOT / 'modules/common/runtime/huggingface-models.json').read_text())
    home = Path(os.environ['HERMES_HOME'])
    seed.seed_config(home, dict(model_ref='huggingface/' + catalog[0]['id'], fallback_models=['huggingface/' + m['id'] for m in catalog[1:]]))
    config = json.loads((home / 'config.yaml').read_text())
    provider = _get_named_custom_provider('agentstack-huggingface', config=config, resolve_credentials=False)
    assert provider['base_url'] == 'https://router.huggingface.co/v1'
    assert provider['key_env'] == 'HF_TOKEN'
    assert not provider.get('api_key')
    assert config['model']['default'] == catalog[0]['id']
    assert [r['model'] for r in config['fallback_providers']] == [m['id'] for m in catalog[1:]]
    overrides = _custom_provider_request_overrides(provider)
    transport = ChatCompletionsTransport()
    reasoning = ' synthetic reasoning fixture\nwith whitespace Ω '
    agent = NS(verbose_logging=False, reasoning_callback=None, stream_delta_callback=None, _stream_callback=None,
               _extract_reasoning=lambda msg: msg.reasoning_content, _strip_think_blocks=lambda text: text,
               _needs_thinking_reasoning_pad=lambda: True, _split_responses_tool_id=lambda ident: (ident, None),
               _derive_responses_function_call_id=lambda ident, item: item)
    results = []
    for model in catalog:
        for count in (1, 2):
            captured = []
            def fake_http(request):
                assert str(request.url) == 'https://router.huggingface.co/v1/chat/completions'
                body = json.loads(request.content)
                captured.append(body)
                tool_turn = len(captured) == 1
                message = dict(role='assistant', content='Checking the fixture.' if tool_turn else 'Fixture complete.', reasoning_content=reasoning)
                if tool_turn:
                    message['tool_calls'] = [dict(id=f'call_fixture_{i}', type='function', function=dict(name='sum_fixture', arguments=json.dumps(dict(a=2+i, b=3)))) for i in range(count)]
                return httpx.Response(200, json=dict(id='offline-fixture', object='chat.completion', created=0, model=model['id'], choices=[dict(index=0, message=message, finish_reason='tool_calls' if tool_turn else 'stop')]))
            with OpenAI(api_key='hf_OfflineFixtureOnly', base_url=provider['base_url'], max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(fake_http))) as client:
                messages = [dict(role='user', content='Exercise the synthetic fixture.')]
                for phase in range(3):
                    kwargs = transport.build_kwargs(model['id'], messages, base_url=provider['base_url'],
                                                    provider_name='custom:agentstack-huggingface', request_overrides=overrides)
                    normalized = transport.normalize_response(client.chat.completions.create(**kwargs))
                    assert normalized.reasoning_content == reasoning
                    row = build_assistant_message(agent, normalized, normalized.finish_reason)
                    assert row['reasoning_content'] == reasoning
                    messages.append(row)
                    if phase == 0:
                        assert len(row['tool_calls']) == count
                        messages.extend(dict(role='tool', tool_call_id=f'call_fixture_{i}', content=str(5+i)) for i in range(count))
                    elif phase == 1:
                        messages.append(dict(role='user', content='Continue this synthetic session.'))
                    # JSON round-trip proves carrier data survives serialization. Native
                    # agent session loading/SQLite/restart are separate, unclaimed checks.
                    messages = json.loads(json.dumps(messages))
                for body in captured:
                    assert body['model'] == model['id']
                    assert body['reasoning_effort'] == 'high'
                    assert body['parallel_tool_calls'] is False
                for body in captured[1:]:
                    assistant = next(m for m in body['messages'] if m['role'] == 'assistant')
                    assert assistant['reasoning_content'] == reasoning
                    assert len(assistant['tool_calls']) == count
                    for i, call in enumerate(assistant['tool_calls']):
                        assert call['id'] == f'call_fixture_{i}'
                        assert json.loads(call['function']['arguments']) == dict(a=2+i, b=3)
                        assert any(m.get('tool_call_id') == call['id'] for m in body['messages'])
                assert [m for m in captured[2]['messages'] if m['role'] == 'assistant'][1]['reasoning_content'] == reasoning
            results.append(dict(id=model['id'], tool_calls=count, status='passed'))
    print(json.dumps(dict(boundary='Native Hermes named-provider resolver, SDK response normalization, history builder, serializer and mocked SDK HTTP body; no full agent turn', real_inference_calls=0, results=results), indent=2))


if __name__ == '__main__':
    if sys.argv[-1:] == ['--worker']:
        worker()
    else:
        source = Path(sys.argv[1]).resolve()
        with tempfile.TemporaryDirectory(prefix='hf-hermes-contract-') as home:
            env = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', TZ='UTC', HERMES_HOME=home,
                       PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE='1')
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker'], cwd=source, env=env, check=True)
