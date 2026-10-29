"""Physics smoke-test checks independent of container availability."""
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('merge_driver', ROOT / 'scripts/test_merging.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class MergeDriverTests(unittest.TestCase):
    def test_normalization_rejects_summed_replica_cross_sections(self):
        inputs = [{'cross_section_pb': 10., 'lhe_events': 100},
                  {'cross_section_pb': 12., 'lhe_events': 200}]
        report = driver.normalization_check(inputs, {'cross_section_pb': 11.33333})
        self.assertAlmostEqual(report['event_weighted_reference_pb'], 34 / 3)
        with self.assertRaisesRegex(ValueError, 'normalization'):
            driver.normalization_check(inputs, {'cross_section_pb': 22.})

    def test_lhco_headers_exclude_banner_and_object_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'events.lhco'
            path.write_text('# 0 0 0\n0 1 0\n1 2 1 2\n 0 2 0\n')
            self.assertEqual(driver.lhco_events(path), 2)

    def test_lhe_weights_support_signed_and_fortran_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'events.lhe.gz'
            with gzip.open(path, 'wt') as stream:
                stream.write('<event>\n2 1 2D+0 0 0 0\n</event>\n<event>\n2 1 -1.0 0 0 0\n</event>\n')
            result = driver.lhe_weights(path)
            self.assertEqual(result, {'events': 2, 'sum_weights': 1., 'sum_abs_weights': 3., 'mean_weight': .5})

    def test_nonfinite_weights_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'events.lhe.gz'
            with gzip.open(path, 'wt') as stream:
                stream.write('<event>\n2 1 nan 0 0 0\n')
            with self.assertRaisesRegex(ValueError, 'Nonfinite'):
                driver.lhe_weights(path)

    def test_failure_report_saved_and_existing_destination_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report'
            command = [sys.executable, str(ROOT / 'scripts/test_merging.py'),
                       '--production', str(Path(temporary) / 'missing'), '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            report = (output / 'report.json').read_bytes()
            self.assertEqual(json.loads(report)['state'], 'failed')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual((output / 'report.json').read_bytes(), report)
