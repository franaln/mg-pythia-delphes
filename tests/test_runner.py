"""Regression checks that do not require physics software or a scheduler."""
import importlib.util
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import json
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_mg_pythia_delphes.py'
spec = importlib.util.spec_from_file_location('runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def config(self, directory):
        card = directory / 'run card.dat'
        card.write_text('')
        return {'run': {'name': 'sample', 'outputs': ['lhe']},
                'process': 'generate p p > t t~', 'cards': {'run': str(card)}}

    def test_validation_rejects_missing_cards_and_bad_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            runner.validate_config(config)
            config['run']['njobs'] = 0
            with self.assertRaises(ValueError):
                runner.validate_config(config)
            config['run']['njobs'] = 1
            config['cards']['run'] = str(Path(tmp) / 'absent')
            with self.assertRaises(ValueError):
                runner.validate_config(config)

    def test_validate_only_does_not_touch_output_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            output = root / 'existing'
            output.mkdir()
            marker = output / 'keep'
            marker.write_text('keep')
            yaml = root / 'config.yml'
            yaml.write_text(runner.yaml.safe_dump(config))
            with patch('sys.argv', [str(SCRIPT), '-c', str(yaml), '-o', str(output),
                                    '--validate-only', '--force']):
                runner.main()
            self.assertEqual(marker.read_text(), 'keep')

    def test_failed_madgraph_is_reported_by_both_wrappers(self):
        for template in (runner.template_run_local_script, runner.template_run_condor_script):
            with self.subTest(condor=template == runner.template_run_condor_script):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    executable = root / 'mg5_aMC'
                    executable.write_text('#!/bin/bash\nexit 42\n')
                    executable.chmod(0o755)
                    (root / 'run.mg5').write_text('')
                    script = root / 'run.sh'
                    script.write_text(template)
                    subprocess.run(['bash', '-n', str(script)], check=True)
                    with tarfile.open(root / 'inputs.tar.gz', 'w:gz') as archive:
                        archive.add(root / 'run.mg5', arcname='run.mg5')
                    env = dict(os.environ, MG_DIR='/unused', PATH=str(root) + ':' + os.environ['PATH'])
                    result = subprocess.run(['bash', str(script), 'sample', 'inputs.tar.gz', 'lhe', 'output'],
                                            cwd=root, env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('ERROR running MadGraph', result.stderr)

    def test_integer_core_count(self):
        self.assertEqual(runner.get_expert_options({'expert': {'ncores': 2}}), ['set nb_core 2'])

    def test_seed_allocation_is_unique_reproducible_and_bounded(self):
        self.assertEqual(runner.allocate_seeds(30080, 3), [30080, 1, 2])
        seeds = runner.allocate_seeds('RANDOM', 100)
        self.assertEqual(len(set(seeds)), 100)
        self.assertTrue(all(1 <= value <= runner.MAX_SEED for value in seeds))
        for seed in (-1, True, 30081, 'bad'):
            with self.assertRaises(ValueError):
                runner.allocate_seeds(seed, 1)
        with self.assertRaises(ValueError):
            runner.allocate_seeds(1, runner.MAX_SEED + 1)

    def test_render_seed_replaces_existing_override(self):
        result = runner.render_seeded_run('launch RUN\nset iseed = 0\nset iseed 23\ndone\n', 17)
        self.assertEqual(result.count('iseed'), 1)
        self.assertTrue(result.endswith('set iseed = 17\ndone\n'))

    def test_preparation_records_all_jobs_for_local_and_condor(self):
        for mode in ('local-docker', 'condor'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.config(root)
                config['run'].update(mode=mode, njobs=3, outputs=['lhe', 'hepmc0'])
                config['options'] = {'seed': 20}
                yaml = root / 'config.yml'
                yaml.write_text(runner.yaml.safe_dump(config))
                output = root / 'production'
                with patch('sys.argv', [str(SCRIPT), '-c', str(yaml), '-o', str(output), '--dry-run']):
                    runner.main()
                manifest = json.loads((output / 'manifest.json').read_text())
                self.assertEqual(manifest['state'], 'prepared')
                self.assertEqual([job['seed'] for job in manifest['jobs']], [20, 21, 22])
                for job in manifest['jobs']:
                    path = output / job['run_file']
                    self.assertIn(f"set iseed = {job['seed']}", path.read_text())
                    self.assertIn(job['run_file'], manifest['prepared_input_sha256'])
                self.assertIn('hepmc', manifest['jobs'][0]['outputs'])
                self.assertNotIn('hepmc', manifest['jobs'][1]['outputs'])
                if mode == 'condor':
                    submission = next(output.glob('*.sub')).read_text()
                    self.assertEqual(submission.count('queue 1'), 3)
                    for job in manifest['jobs']:
                        self.assertIn(job['run_file'], submission)
                else:
                    self.assertEqual(len(list(output.glob('run_*/run_mg_pythia_delphes.sh'))), 3)

    def test_condor_worker_uses_prepared_seed_and_lhe_only_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'run.mg5').write_text('set iseed = 0\ndone\n')
            with tarfile.open(root / 'inputs.tar.gz', 'w:gz') as archive:
                archive.add(root / 'run.mg5', arcname='run.mg5')
            (root / 'seeded.mg5').write_text('set iseed = 123\ndone\n')
            executable = root / 'mg5_aMC'
            executable.write_text('#!/bin/bash\ncat run.mg5 > actual.mg5\n'
                                  'mkdir -p RUN/Events/run_01\n'
                                  'touch RUN/Events/run_01/unweighted_events.lhe.gz\n')
            executable.chmod(0o755)
            script = root / 'run.sh'
            script.write_text(runner.template_run_condor_script)
            env = dict(os.environ, MG_DIR='/unused', PATH=str(root) + ':' + os.environ['PATH'])
            result = subprocess.run(['bash', str(script), 'sample', 'inputs.tar.gz',
                                     'lhe', 'output', 'seeded.mg5'], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'actual.mg5').read_text(), (root / 'seeded.mg5').read_text())
            with tarfile.open(root / 'output.tar.gz') as archive:
                self.assertIn('output_unweighted_events.lhe.gz', archive.getnames())

    def test_local_execution_failure_updates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yaml = root / 'config.yml'
            yaml.write_text(runner.yaml.safe_dump(self.config(root)))
            output = root / 'production'
            with patch('sys.argv', [str(SCRIPT), '-c', str(yaml), '-o', str(output)]), \
                 patch.object(runner.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')):
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.main()
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['state'], 'failed')
            self.assertEqual(manifest['failed_job_id'], 'sample_00000')

    def test_programmatic_preparation_is_nonexecuting_and_does_not_mutate_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            config['run']['njobs'] = 2
            config['options'] = {'seed': 101}
            config['expert'] = {'ncores': 'all'}
            original = copy.deepcopy(config)
            cwd = Path.cwd()
            with patch.object(runner.subprocess, 'run') as launch:
                production = runner.prepare_production(config, root / 'production')
                launch.assert_not_called()
            self.assertEqual(config, original)
            self.assertEqual(Path.cwd(), cwd)
            self.assertEqual([job['seed'] for job in production.manifest['jobs']], [101, 102])
            self.assertEqual(runner.read_production_manifest(production.output_dir), production.manifest)
            self.assertEqual(production.manifest['state'], 'prepared')

    def test_programmatic_execution_uses_prepared_commands_and_seeds(self):
        for mode in ('local-docker', 'local-apptainer', 'condor', 'jupiter'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.config(root)
                config['run'].update(mode=mode, njobs=2, image='mg-pythia-delphes-latest')
                config['options'] = {'seed': 203}
                production = runner.prepare_production(config, root / 'production')
                frozen = {job['run_file']: (Path(production.output_dir) / job['run_file']).read_bytes()
                          for job in production.manifest['jobs']}
                with patch.object(runner, 'allocate_seeds', side_effect=AssertionError('must not reallocate')), \
                     patch.object(runner.subprocess, 'run') as launch:
                    result = runner.execute_production(production)
                if mode in ('condor', 'jupiter'):
                    launch.assert_called_once_with(['condor_submit', production.submit_file],
                                                   cwd=production.output_dir, check=True)
                else:
                    self.assertEqual(launch.call_count, 2)
                    for invocation in launch.call_args_list:
                        self.assertEqual(invocation.args[0][0], mode.removeprefix('local-'))
                    self.assertEqual(result['state'], 'execution_finished')
                    self.assertEqual(runner.read_production_manifest(production.output_dir)['state'],
                                     'execution_finished')
                for path, contents in frozen.items():
                    self.assertEqual((Path(production.output_dir) / path).read_bytes(), contents)

    def test_library_errors_do_not_exit_or_replace_existing_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            output = root / 'production'
            output.mkdir()
            marker = output / 'keep'
            marker.write_text('preserve')
            with self.assertRaises(FileExistsError):
                runner.prepare_production(config, output)
            self.assertEqual(marker.read_text(), 'preserve')
            config['run'].update(mode='condor', image='unsupported')
            with self.assertRaises(ValueError):
                runner.prepare_production(config, root / 'invalid')
            self.assertFalse((root / 'invalid').exists())

    def test_load_config_applies_mode_override_without_rewriting_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            source = root / 'config.yml'
            text = runner.yaml.safe_dump(config)
            source.write_text(text)
            loaded = runner.load_config(source, run_mode='condor')
            self.assertEqual(loaded['run']['mode'], 'condor')
            self.assertEqual(source.read_text(), text)



if __name__ == '__main__':
    unittest.main()
