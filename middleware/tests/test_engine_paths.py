"""Binary selection after moving the middleware outside the NeuG tree."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico.engine import resolve_binary


class EnginePathTests(unittest.TestCase):
    def test_patch_precedence_for_engine_and_executor(self):
        for name in ('incgq_rewrite_engine', 'cico_exec'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for folder, variant in [('build', name), ('bin', name + '_patched')]:
                    binary = root / folder / variant
                    binary.parent.mkdir(exist_ok=True)
                    binary.touch(mode=0o755)
                with patch('cico.engine._ENGINE_DIR', root), patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(resolve_binary(name, 'CICO_TEST_BINARY'), root / 'bin' / (name + '_patched'))
                    local = root / 'build' / (name + '_patched')
                    local.touch(mode=0o755)
                    self.assertEqual(resolve_binary(name, 'CICO_TEST_BINARY'), local)

    def test_override_is_resolved_even_when_missing(self):
        with patch.dict(os.environ, {'CICO_TEST_BINARY': './missing-engine'}):
            self.assertEqual(resolve_binary('ignored', 'CICO_TEST_BINARY'), Path('missing-engine').resolve())

    def test_non_executable_copy_does_not_hide_a_working_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'build').mkdir()
            (root / 'bin').mkdir()
            (root / 'build/engine_patched').touch(mode=0o644)
            fallback = root / 'bin/engine'
            fallback.touch(mode=0o755)
            with patch('cico.engine._ENGINE_DIR', root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_binary('engine', 'CICO_TEST_BINARY'), fallback)
