import importlib.util
from pathlib import Path
import json
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[3] / "modules/common/runtime/hermes_hf_tool_compat.py"
spec = importlib.util.spec_from_file_location("hermes_hf_tool_content", SOURCE)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class HermesToolContentTest(unittest.TestCase):
    def test_native_hooks_convert_only_muse_json_content_on_the_hf_route(self):
        hooks = {}

        class Context:
            def register_hook(self, name, hook):
                hooks[name] = hook

        plugin.register(Context())
        args = {"path": "/owned/result", "content": {"sum": 42}}
        track = hooks["pre_api_request"]
        normalize = hooks["pre_tool_call"]
        for model, endpoint in (("other", "https://router.huggingface.co/v1"), (plugin.MUSE, "https://other.invalid/v1")):
            track(session_id="s", model=model, base_url=endpoint)
            self.assertIsNone(normalize(tool_name="write_file", args=args, session_id="s"))
        track(session_id="s", model=plugin.MUSE, base_url="https://router.huggingface.co/v1")
        self.assertEqual(normalize(tool_name="write_file", args=args, session_id="s"), {"action": "modify", "args": {"content": '{"sum":42}'}})
        self.assertEqual(args, {"path": "/owned/result", "content": {"sum": 42}})
        self.assertIsNone(normalize(tool_name="terminal", args=args, session_id="s"))
        for content in ("already text", None, 42):
            self.assertIsNone(normalize(tool_name="write_file", args={"content": content}, session_id="s"))
        hooks["on_session_end"](session_id="s")
        self.assertIsNone(normalize(tool_name="write_file", args=args, session_id="s"))

    def test_installers_preserve_other_plugins_and_refuse_directory_symlinks(self):
        for harness, directory in (("hermes", "plugins"), ("openclaw", "extensions")):
            spec = importlib.util.spec_from_file_location(harness + "_compat_config", SOURCE.with_name(harness + "_config.py"))
            managed = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(managed)
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = {"plugins": {"enabled": ["operator"]}} if harness == "hermes" else {"plugins": {"allow": ["operator"], "entries": {"operator": {"enabled": True}}}}
                managed.install_tool_compat(root, config)
                original = json.dumps(config, sort_keys=True)
                files = {p.name: p.read_bytes() for p in (root / directory / managed.COMPAT_ID).iterdir()}
                managed.install_tool_compat(root, config)
                self.assertEqual(json.dumps(config, sort_keys=True), original)
                self.assertEqual(files, {p.name: p.read_bytes() for p in (root / directory / managed.COMPAT_ID).iterdir()})
                self.assertIn("operator", config["plugins"]["enabled" if harness == "hermes" else "allow"])
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                outside = root / "outside"
                outside.mkdir()
                (root / directory).symlink_to(outside, target_is_directory=True)
                with self.assertRaises(OSError):
                    managed.install_tool_compat(root, {})
                self.assertEqual(list(outside.iterdir()), [])
