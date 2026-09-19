"""Reuse the production SDK login without changing accounts or exposing keys."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deepline_http as transport


class DeeplineAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.scoped = self.root / ".local/deepline/code-deepline-com/.env"
        self.scoped.parent.mkdir(parents=True)
        self.scoped.write_text("DEEPLINE_API_KEY='scoped-key'\n")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(transport.Path, "home", return_value=self.root).start()
        patch.object(transport.Path, "cwd", return_value=self.project).start()

    def test_environment_then_matching_project_then_scoped_key(self):
        self.assertEqual(transport.api_key(), "scoped-key")
        (self.project / ".env.deepline").write_text(
            'DEEPLINE_HOST_URL=https://code.deepline.com/\nDEEPLINE_API_KEY="project-key"\n')
        self.assertEqual(transport.api_key(), "project-key")
        with patch.dict(os.environ, {"DEEPLINE_API_KEY": "env-key"}):
            self.assertEqual(transport.api_key(), "env-key")

    def test_other_host_or_custom_cli_never_sends_its_key_to_production(self):
        for env in ({"DEEPLINE_HOST_URL": "https://other.test", "DEEPLINE_API_KEY": "other-key"},
                    {"DEEPLINE_BIN": "/custom/deepline"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assertIsNone(transport.api_key())
        (self.project / ".env.deepline").write_text(
            "DEEPLINE_HOST_URL=https://other.test\nDEEPLINE_API_KEY=other-key\n")
        self.assertIsNone(transport.api_key())
        with patch.dict(os.environ, {"DEEPLINE_HOST_URL": transport.API_HOST}):
            self.assertEqual(transport.api_key(), "scoped-key")

    def test_nearest_project_only_and_no_shell_evaluation(self):
        (self.root / ".env.deepline").write_text(
            "DEEPLINE_HOST_URL=https://code.deepline.com\nDEEPLINE_API_KEY=parent-key\n")
        self.assertEqual(transport.api_key(), "parent-key")
        (self.project / ".env.deepline").write_text(
            "DEEPLINE_HOST_URL=https://code.deepline.com\nDEEPLINE_API_KEY=$(never-execute)\n")
        self.assertEqual(transport.api_key(), "$(never-execute)")

    def test_missing_login_retains_cli_fallback(self):
        self.scoped.unlink()
        self.assertIsNone(transport.api_key())
