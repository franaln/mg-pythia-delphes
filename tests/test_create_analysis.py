"""Editable starter creation and the documented analyse entry point."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mgpd.analysis import analysis_example
from mgpd.cli import main, parse_args
from mgpd.analysis.config import load_analysis_config


class CreateAnalysisTests(unittest.TestCase):
    def test_default_starter_from_installed_command_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, '-m', 'mgpd', 'create-analysis'],
                                    cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = Path(tmp) / 'analysis.yml'
            self.assertEqual(path.read_text(), analysis_example('dileptons-root'))
            self.assertIn('mgpd analyse', result.stdout)
            self.assertEqual(load_analysis_config(path)[0]['input']['format'], 'root')
            # Re-running must preserve edits, rather than replace the file.
            path.write_text('user edits')
            result = subprocess.run([sys.executable, '-m', 'mgpd', 'create-analysis'],
                                    cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(), 'user edits')

    def test_all_starters_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            for format in ('root', 'lhe', 'lhco'):
                path = Path(tmp) / (format + '.yml')
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['create-analysis', str(path), '--format', format]), 0)
                self.assertEqual(load_analysis_config(path)[0]['input']['format'], format)
                for alias in ('analyze', 'analyse', 'ana'):
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main([alias, '--example', 'dileptons-' + format]), 0)
                    self.assertEqual(output.getvalue(), path.read_text())
            for alias in ('analyse', 'ana'):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_args([alias])
