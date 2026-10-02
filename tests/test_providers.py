import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.providers import ProviderError, load_providers, resolve_llm_config


class TestProviders(unittest.TestCase):
    def _root(self, td: Path) -> Path:
        (td / "config").mkdir(parents=True, exist_ok=True)
        (td / "config" / "providers.yaml").write_text(
            """
default: p1
providers:
  p1:
    base_url: https://api.example.com/v1
    api_key_env: KEY_A
    model: model-a
    thinking: true
    reasoning_effort: max
    timeout_sec: 30
    max_tokens: 1000
  p2:
    base_url: http://gw:1/v1
    api_key_env: KEY_B
    model: model-b
    thinking: false
    json_mode: false
""",
            encoding="utf-8",
        )
        return td

    def test_load_and_default(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root(Path(t))
            reg = load_providers(root)
            self.assertEqual(reg["default"], "p1")
            cfg = resolve_llm_config(root, bot_id="bot1", llm={})
            self.assertEqual(cfg.model, "model-a")
            self.assertEqual(cfg.api_key_env, "KEY_A")
            self.assertTrue(cfg.thinking)
            self.assertEqual(cfg.max_tokens, 1000)

    def test_provider_override(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root(Path(t))
            cfg = resolve_llm_config(root, bot_id="b", llm={"provider": "p2"})
            self.assertEqual(cfg.model, "model-b")
            self.assertFalse(cfg.thinking)
            self.assertFalse(cfg.json_mode)
            self.assertEqual(cfg.default_base_url, "http://gw:1/v1")

    def test_bot_field_overrides_provider(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root(Path(t))
            cfg = resolve_llm_config(root, bot_id="b", llm={"provider": "p1", "model": "model-x", "thinking": False})
            self.assertEqual(cfg.model, "model-x")
            self.assertFalse(cfg.thinking)

    def test_unknown_provider(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root(Path(t))
            with self.assertRaises(ProviderError):
                resolve_llm_config(root, llm={"provider": "nope"})


if __name__ == "__main__":
    unittest.main()
