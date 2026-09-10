"""Plugin registry and replacement boundary tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_forge.contracts import DomainError, ErrorCode
from code_forge.runtime.plugins import PluginRegistry, build_builtin_registry, build_plugins


class DummyContext:
    def build(self, system_prompt, history, current_input):
        return [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": current_input},
        ]


class PluginRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_builtin_plugins_wire_local_stack(self):
        plugins = build_plugins(
            self.root,
            {
                "FORGE_MODEL": "none",
                "FORGE_HARNESS": "local",
                "FORGE_CONTEXT": "bounded",
                "FORGE_MAX_TOOL_STEPS": "5",
            },
        )
        self.assertIsNone(plugins.model_adapter)
        self.assertEqual(type(plugins.harness).__name__, "LocalDeterministicHarness")
        self.assertEqual(type(plugins.context_manager).__name__, "ConversationContextManager")

    def test_custom_plugin_can_replace_builtin(self):
        registry = build_builtin_registry()
        registry.register("context", "dummy", lambda **_: DummyContext())
        plugins = build_plugins(
            self.root,
            {
                "FORGE_MODEL": "none",
                "FORGE_HARNESS": "local",
                "FORGE_CONTEXT": "dummy",
            },
            registry=registry,
        )
        self.assertIsInstance(plugins.context_manager, DummyContext)

    def test_unknown_plugin_is_explicit_dependency_error(self):
        with self.assertRaises(DomainError) as error:
            PluginRegistry().create("store", "missing")
        self.assertEqual(error.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
