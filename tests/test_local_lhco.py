"""Execute generated local Bash with fake physics tools, checking LHCO behavior."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from mgpd.preparation import prepare_wrappers


class LocalLhcoTests(unittest.TestCase):
    def run_wrapper(self, outputs, converter, decayed=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        job = root / 'job with spaces'
        job.mkdir()
        tools = root / 'tools'
        tools.mkdir()
        run = 'run_01_decayed_1' if decayed else 'run_01'
        mg = tools / 'mg5_aMC'
        mg.write_text(f'#!/bin/bash\nmkdir -p RUN/Events/{run}\nprintf root > RUN/Events/{run}/sample_delphes_events.root\nprintf banner > RUN/Events/{run}/{run}_sample_banner.txt\n')
        mg.chmod(0o755)
        if converter is not None:
            tool = tools / 'root2lhco'
            tool.write_text('#!/bin/bash\n' + converter)
            tool.chmod(0o755)
        prepare_wrappers(root, {'job': ('sample', str(job))}, 'local-apptainer', outputs)
        env = dict(os.environ, MG_DIR='/unused', PATH=str(tools) + ':/usr/bin:/bin')
        result = subprocess.run(['bash', str(job / 'run_mg_pythia_delphes.sh'), 'sample', 'unused'],
                                cwd=job, env=env, capture_output=True, text=True)
        return result, job / 'RUN/Events' / run

    def test_requested_lhco_is_created_for_normal_and_madspin_runs(self):
        for decayed in (False, True):
            with self.subTest(decayed=decayed):
                result, events = self.run_wrapper(['root', 'lhco'], 'printf "0 1 0\\n" > "$2"\n', decayed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((events / 'sample_delphes_events.lhco').read_text(), '# banner\n0 1 0\n')
                self.assertEqual((events / 'sample_delphes_events.root').read_text(), 'root')

    def test_no_converter_needed_when_lhco_not_requested(self):
        result, events = self.run_wrapper(['root', 'lhe'], None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(events.glob('*.lhco')))

    def test_failed_converter_and_missing_or_empty_output_fail_the_job(self):
        for converter, message in ((None, 'unavailable'), ('exit 42\n', 'root2lhco failed'),
                                   ('exit 0\n', 'no LHCO output'), (': > "$2"\n', 'no LHCO output')):
            with self.subTest(converter=converter):
                result, events = self.run_wrapper(['lhco'], converter)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertTrue((events / 'sample_delphes_events.root').exists())
