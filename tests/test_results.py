"""Artifact validation with real file formats; never run generators or schedulers."""
from contextlib import redirect_stdout
import gzip
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = 'mgpd'
from mgpd import compat as runner


def lhe(events=2, seed=101, cross=10, closed=True):
    text = f'''<LesHouchesEvents>
<header>
{seed} = iseed
</header>
<init>
2212 2212 6500 6500 0 0 0 0 3 1
{cross} 0.1 1 1
<generator name="MG">version</generator>
</init>
''' + '<event>\n</event>\n' * events
    if closed:
        text += '</LesHouchesEvents>\n'
    return gzip.compress(text.encode())


class ResultsTests(unittest.TestCase):
    def fixture(self, root, mode='local-apptainer', count=1, outputs=None):
        jobs = [{'job_id': f'sample_{i}', 'sample': 'sample', 'index': i, 'seed': 101 + i,
                 'nevents': 2, 'outputs': outputs or ['lhe'], 'run_file': f'run_sample_{i}/run.mg5'}
                for i in range(count)]
        manifest = {'schema_version': 2, 'production_id': 'a' * 32, 'mode': mode,
                    'state': 'submitted' if mode == 'condor' else 'execution_finished', 'jobs': jobs}
        if mode == 'condor':
            manifest['submission'] = {'jobs': [{'job_id': job['job_id'], 'scheduler_id': f'123.{i}'}
                                                for i, job in enumerate(jobs)]}
        else:
            for job in jobs:
                directory = root / Path(job['run_file']).parent / 'RUN/Events/run_01'
                directory.mkdir(parents=True)
                (directory / 'unweighted_events.lhe.gz').write_bytes(lhe(seed=job['seed']))
        runner.write_manifest(root, manifest)
        return manifest

    def archive(self, root, index=0, contents=None):
        with tarfile.open(root / f'output_sample_123_{index}.tar.gz', 'w:gz') as archive:
            for name, data in (contents or [(f'output_sample_123_{index}_unweighted_events.lhe.gz', lhe(seed=101 + index))]):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        (root / f'job_sample_123_{index}.log').write_text('Normal termination (return value 0)\n')

    def test_condor_replicas_have_event_totals_but_cross_sections_are_not_summed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, 'condor', count=2)
            self.archive(root, 0)
            self.archive(root, 1)
            before = (root / 'manifest.json').read_bytes()
            with patch.object(subprocess, 'run', side_effect=AssertionError('No scheduler calls')):
                result = runner.validate_production(root)
            self.assertEqual(result['state'], 'valid')
            self.assertEqual(result['samples']['sample']['validated_lhe_events'], 4)
            self.assertEqual([entry['value'] for entry in result['samples']['sample']['cross_sections_pb']], [10, 10])
            self.assertEqual((root / 'manifest.json').read_bytes(), before)

    def test_missing_archive_and_unmapped_job_are_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self.fixture(root, 'condor')
            self.assertEqual(runner.validate_production(root)['counts'], {'missing': 1})
            manifest.pop('submission')
            runner.write_manifest(root, manifest)
            result = runner.validate_production(root)
            self.assertEqual(result['state'], 'incomplete')
            self.assertEqual(result['counts'], {'unavailable': 1})

    def test_missing_requested_output_does_not_hide_readable_lhe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, outputs=['lhe', 'root'])
            result = runner.validate_production(root)
            self.assertEqual(result['state'], 'incomplete')
            self.assertEqual(result['jobs'][0]['metrics']['lhe_events'], 2)
            self.assertEqual(result['jobs'][0]['outputs']['root']['state'], 'missing')
            self.assertEqual(result['samples']['sample']['validated_lhe_events'], 0)

    def test_truncated_lhe_and_wrong_seed_are_invalid(self):
        for data in (lhe(closed=False), lhe(seed=42), lhe()[:-10]):
            with self.subTest(data=data[-12:]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                (root / 'run_sample_0/RUN/Events/run_01/unweighted_events.lhe.gz').write_bytes(data)
                self.assertEqual(runner.validate_production(root)['state'], 'invalid')

    def test_relaxed_policy_is_explicit_and_never_allows_nonfinite_cross_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            path = root / 'run_sample_0/RUN/Events/run_01/unweighted_events.lhe.gz'
            path.write_bytes(lhe(events=1, seed=999, cross=-2))
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')
            result = runner.validate_production(root, event_count='nonempty',
                                                require_positive_cross_section=False, check_seed=False)
            self.assertEqual(result['state'], 'valid')
            self.assertFalse(result['policy']['check_seed'])
            self.assertEqual(result['jobs'][0]['metrics']['cross_section_pb'], -2)
            path.write_bytes(lhe(cross='nan'))
            self.assertEqual(runner.validate_production(root, require_positive_cross_section=False)['state'], 'invalid')

    def test_ignore_count_allows_empty_but_well_formed_lhe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            path = root / 'run_sample_0/RUN/Events/run_01/unweighted_events.lhe.gz'
            path.write_bytes(lhe(events=0))
            self.assertEqual(runner.validate_production(root, event_count='nonempty')['state'], 'invalid')
            self.assertEqual(runner.validate_production(root, event_count='ignore')['state'], 'valid')

    def test_failed_execution_is_distinct_from_valid_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, 'condor')
            self.archive(root)
            (root / 'job_sample_123_0.log').write_text('Normal termination (return value 0)\nJob was aborted\n')
            job = runner.validate_production(root)['jobs'][0]
            self.assertEqual(job['outputs_state'], 'valid')
            self.assertEqual(job['execution_state'], 'failed')
            self.assertEqual(job['state'], 'invalid')

    def test_corrupt_archive_and_duplicate_basenames_are_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, 'condor')
            path = root / 'output_sample_123_0.tar.gz'
            path.write_bytes(b'not a tar file')
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')
            self.archive(root, contents=[('one/x_unweighted_events.lhe.gz', lhe()),
                                         ('two/x_unweighted_events.lhe.gz', lhe())])
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')

    def test_hepmc_crc_is_checked_through_end_of_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, outputs=['hepmc'])
            path = root / 'run_sample_0/RUN/Events/run_01/sample_pythia8_events.hepmc.gz'
            data = gzip.compress(b'HepMC::Version 2.06.09\n' + b'E 1\n' * 5000)
            path.write_bytes(data)
            self.assertEqual(runner.validate_production(root)['state'], 'valid')
            corrupt = bytearray(data)
            corrupt[-8] ^= 1
            path.write_bytes(corrupt)
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')

    def test_missing_root_dependency_is_unavailable_not_bad_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, outputs=['root'])
            path = root / 'run_sample_0/RUN/Events/run_01/sample_delphes_events.root'
            path.write_bytes(b'placeholder')
            with patch.dict(sys.modules, {'uproot': None}):
                result = runner.validate_production(root)
            self.assertEqual(result['state'], 'incomplete')
            self.assertEqual(result['counts'], {'unavailable': 1})

    @unittest.skipUnless(importlib.util.find_spec('uproot'), 'Optional uproot not installed')
    def test_real_root_tree_and_empty_tree(self):
        import uproot
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, outputs=['root'])
            path = root / 'run_sample_0/RUN/Events/run_01/sample_delphes_events.root'
            with uproot.recreate(path) as output:
                tree = output.mktree('Delphes', {'Particle.PT': 'float64'})
                tree.extend({'Particle.PT': np.array([10., 20.])})
            result = runner.validate_production(root)
            self.assertEqual(result['state'], 'valid')
            self.assertEqual(result['samples']['sample']['validated_delphes_events'], 2)
            with uproot.recreate(path) as output:
                output.mktree('Delphes', {'Particle.PT': 'float64'})
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')
            path.write_bytes(b'not a ROOT file')
            self.assertEqual(runner.validate_production(root)['state'], 'invalid')

    def test_madspin_decayed_directory_and_local_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, outputs=['lhe', 'log'])
            base = root / 'run_sample_0/RUN'
            (base / 'Events/run_01').rename(base / 'Events/run_01_decayed_1')
            (base / 'generation.log').write_text('finished')
            result = runner.validate_production(root)
            self.assertEqual(result['state'], 'valid')
            self.assertEqual(result['jobs'][0]['metrics']['log_files'], 1)
            self.assertIn('run_01_decayed_1', result['jobs'][0]['location'])

    def test_validation_cli_json_and_policy_flag_scoping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = runner.main(['validate-results', str(root), '--json'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['state'], 'valid')
            with self.assertRaises(SystemExit):
                runner.main(['status', str(root), '--event-count', 'ignore'])


if __name__ == '__main__':
    unittest.main()
