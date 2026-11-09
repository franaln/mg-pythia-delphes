"""Prepare real plans and run worker Bash; fake only external physics programs.

This checks wrapper/configuration/artifact contracts. Tiny ROOT placeholders do
not validate ROOT data, container environments, physics or a live scheduler.
"""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import mgpd
from mgpd.results import result_files, inspect_outputs


from image_fixtures import installed_image

class OutputContractTests(unittest.TestCase):
    def setUp(self):
        installed_image(self)

    def production(self, root, mode, outputs, *, decayed=False, missing=None, converter='ok', assembly_failure=None, output_failure=None):
        tools = root / 'tools'
        tools.mkdir()
        # The external simulator knows nothing about requested outputs: it emits
        # native MG/Delphes files. Only the real wrapper can create final LHCO.
        simulator = tools / 'mg5_aMC'
        simulator.write_text(f'''#!{sys.executable}
import gzip, os
from pathlib import Path
out = Path('RUN/Events/{'run_01_decayed_1' if decayed else 'run_01'}')
out.mkdir(parents=True)
files = {{'sample_delphes_events.root': b'ROOT-placeholder',
         'unweighted_events.lhe.gz': gzip.compress(b'LHE-placeholder'),
         'sample_pythia8_events.hepmc.gz': gzip.compress(b'HepMC-placeholder'),
         '{'run_01_decayed_1' if decayed else 'run_01'}_sample_banner.txt': b'banner'}}
for name, data in files.items():
    if name != {missing!r}:
        (out / name).write_bytes(data)
if {decayed!r}:
    before = Path('RUN/Events/run_01')
    before.mkdir(exist_ok=True)
    (before / 'unweighted_events.lhe').write_bytes(b'pre-decay LHE')
Path('RUN/generation.log').write_text('generator log')
Path('executed.mg5').write_bytes(Path('run.mg5').read_bytes())
''')
        simulator.chmod(0o755)
        if converter != 'absent':
            script = tools / 'root2lhco'
            actions = {'ok': 'printf "0 1 0\\n" > "$2"\n',
                       'fail': 'exit 42\n', 'silent': 'exit 0\n',
                       'partial': 'printf partial > "$2"\nexit 42\n',
                       'empty': ': > "$2"\n'}
            script.write_text('#!/bin/bash\n' + actions[converter])
            script.chmod(0o755)
        if assembly_failure:
            tool = tools / assembly_failure
            # Seed substitution and native ROOT/LHE moves must still work.
            action = ('case "${@: -1}" in *.lhco) exit 42;; esac\n'
                      'exec /bin/mv "$@"\n') if assembly_failure == 'mv' else 'exit 42\n'
            if assembly_failure == 'sed':
                action = 'case "${@: -1}" in *_banner.txt) exit 42;; esac\nexec /bin/sed "$@"\n'
            tool.write_text('#!/bin/bash\n' + action)
            tool.chmod(0o755)
        if output_failure:
            command, pattern = output_failure
            tool = tools / command
            tool.write_text('#!/bin/bash\ncase "$*" in *' + pattern +
                            '*) exit 42;; esac\nexec /usr/bin/' + command + ' "$@"\n')
            tool.chmod(0o755)
        card = root / 'card.dat'
        card.write_text('')
        config = {'run': {'name': 'sample', 'mode': mode, 'image': 'mg-pythia-delphes-3.5.6',
                          'njobs': 1, 'nevents': 1},
                  'process': 'generate p p > e+ e-',
                  'cards': {'run': str(card), 'pythia': str(card), 'delphes': str(card)},
                  'options': {'seed': 101}}
        if outputs is not None:
            config['run']['outputs'] = outputs
        with redirect_stdout(io.StringIO()):
            prepared = mgpd.prepare_production(config, root / 'production')
        loaded = mgpd.load_prepared_production(prepared.output_dir)
        job = loaded.manifest['jobs'][0]
        env = dict(os.environ, COPYFILE_DISABLE='1', MG_DIR='/unused', PATH=str(tools) + ':/usr/bin:/bin')
        if mode == 'condor':
            work = root / 'worker'
            work.mkdir()
            for relative in (f'run_{job["sample"]}.tar.gz', 'run_mgpd_condor.sh'):
                source = Path(loaded.output_dir) / relative
                shutil.copyfile(source, work / source.name)
            command = ['bash', str(work / 'run_mgpd_condor.sh'), job['sample'],
                       f'run_{job["sample"]}.tar.gz', ' '.join(job['outputs']),
                       'output_sample_123_0', str(job['seed'])]
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
            return result, loaded, job, work
        # Bypass only the container boundary. Run the generated wrapper via the
        # actual saved-plan executor, including checksum and attempt guards.
        def native(mode, image, sample, directory):
            return ['bash', '-c', 'cd ' + shlex.quote(directory) + ' && exec bash ./run_mgpd_local.sh ' + shlex.quote(sample) + ' unused']
        with patch('mgpd.execution.build_local_command', side_effect=native), patch.dict(os.environ, env):
            try:
                mgpd.execute_production(loaded)
            except subprocess.CalledProcessError as exc:
                return exc, loaded, job, Path(loaded.output_dir) / Path(job['run_file']).parent
        return None, loaded, job, Path(loaded.output_dir) / Path(job['run_file']).parent

    def test_requested_output_contract_across_worker_modes_and_selections(self):
        selections = [['lhe'], ['root'], ['lhco'], ['hepmc'], ['hepmc0'], ['log'], ['all'],
                      ['lhe', 'root', 'lhco', 'hepmc', 'log'], None]
        for mode in ('local-apptainer', 'local-docker', 'condor'):
            for outputs in selections:
                for decayed in (False, True):
                    with self.subTest(mode=mode, outputs=outputs, decayed=decayed), tempfile.TemporaryDirectory() as tmp:
                        result, prepared, job, work = self.production(Path(tmp), mode, outputs, decayed=decayed)
                        self.assertTrue(result is None or result.returncode == 0,
                                        getattr(result, 'stderr', str(result)))
                        self.assertIn('set iseed = 101', (work / 'executed.mg5').read_text())
                        if mode == 'condor':
                            shutil.copyfile(work / 'output_sample_123_0.tar.gz',
                                            Path(prepared.output_dir) / 'output_sample_123_0.tar.gz')
                        suffixes = {'lhe': '_unweighted_events.lhe.gz', 'root': '_delphes_events.root',
                                    'lhco': '_delphes_events.lhco', 'hepmc': '_pythia8_events.hepmc.gz',
                                    'log': '_logs.tar'}
                        with result_files(prepared.output_dir, prepared.manifest, job, '123.0') as (files, _, log_count):
                            if job['outputs'] == ['all']:
                                # all retains native files; it does not request LHCO conversion.
                                self.assertIn('unweighted_events.lhe.gz' if mode == 'condor' else 'local_unweighted_events.lhe.gz', files)
                                self.assertTrue(any(name.endswith('_delphes_events.root') for name in files))
                                continue
                            for kind in job['outputs']:
                                if kind == 'log' and mode != 'condor':
                                    self.assertGreater(log_count, 0)
                                    continue
                                matches = [name for name in files if name.endswith(suffixes[kind])]
                                self.assertEqual(len(matches), 1, (kind, sorted(files)))
                                with files[matches[0]]() as stream:
                                    self.assertTrue(stream.read(1), kind)

    def test_lhco_failure_preserves_other_outputs_and_remains_invalid(self):
        for mode in ('local-apptainer', 'local-docker', 'condor'):
            for converter in ('absent', 'fail', 'silent', 'empty', 'partial'):
                for outputs in (['lhco'], ['lhco', 'root', 'lhe', 'hepmc', 'log']):
                    with self.subTest(mode=mode, converter=converter, outputs=outputs), tempfile.TemporaryDirectory() as tmp:
                        result, prepared, job, work = self.production(Path(tmp), mode, outputs, converter=converter)
                        self.assertTrue(result is None or result.returncode == 0,
                                        getattr(result, 'stderr', str(result)))
                        if mode == 'condor':
                            self.assertIn('WARNING:', result.stderr)
                            shutil.copyfile(work / 'output_sample_123_0.tar.gz',
                                            Path(prepared.output_dir) / 'output_sample_123_0.tar.gz')
                        else:
                            self.assertEqual(mgpd.read_production_manifest(prepared.output_dir)['state'],
                                             'execution_finished')
                        with result_files(prepared.output_dir, prepared.manifest, job, '123.0') as (files, _, _):
                            self.assertFalse(any(name.endswith('.lhco') for name in files))
                            for kind, suffix in [('root', '_delphes_events.root'),
                                                 ('lhe', '_unweighted_events.lhe.gz'),
                                                 ('hepmc', '_pythia8_events.hepmc.gz')]:
                                if kind in outputs:
                                    self.assertTrue(any(name.endswith(suffix) for name in files), kind)
                            # Validate missing LHCO independently of fake ROOT/LHE contents.
                            with self.assertRaisesRegex(ValueError, 'Expected one lhco output'):
                                inspect_outputs(files, dict(job, outputs=['lhco']))

    def test_lhco_assembly_failure_preserves_requested_native_outputs(self):
        for mode in ('local-apptainer', 'local-docker', 'condor'):
            for failure in ('sed', 'cat', 'mv'):
                with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    result, prepared, job, work = self.production(
                        Path(tmp), mode, ['lhco', 'root', 'lhe'], assembly_failure=failure)
                    self.assertTrue(result is None or result.returncode == 0,
                                    getattr(result, 'stderr', str(result)))
                    if mode == 'condor':
                        self.assertIn('WARNING: LHCO assembly failed', result.stderr)
                        shutil.copyfile(work / 'output_sample_123_0.tar.gz',
                                        Path(prepared.output_dir) / 'output_sample_123_0.tar.gz')
                    with result_files(prepared.output_dir, prepared.manifest, job, '123.0') as (files, _, _):
                        self.assertTrue(any(name.endswith('_delphes_events.root') for name in files))
                        self.assertTrue(any(name.endswith('_unweighted_events.lhe.gz') for name in files))
                        self.assertFalse(any(name.endswith('.lhco') for name in files))
                        with self.assertRaisesRegex(ValueError, 'Expected one lhco output'):
                            inspect_outputs(files, dict(job, outputs=['lhco']))

    def test_missing_condor_banner_does_not_discard_root_or_lhe(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _, work = self.production(Path(tmp), 'condor', ['lhco', 'root', 'lhe'],
                                                missing='run_01_sample_banner.txt')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('WARNING: LHCO assembly failed', result.stderr)
            with tarfile.open(work / 'output_sample_123_0.tar.gz') as archive:
                names = archive.getnames()
            self.assertTrue(any(name.endswith('_delphes_events.root') for name in names))
            self.assertTrue(any(name.endswith('_unweighted_events.lhe.gz') for name in names))
            self.assertFalse(any(name.endswith('.lhco') for name in names))

    def test_missing_native_output_does_not_discard_other_formats(self):
        outputs = ['root', 'lhe', 'hepmc', 'log', 'lhco']
        for kind, filename, suffix in [('lhe', 'unweighted_events.lhe.gz', '_unweighted_events.lhe.gz'),
                                      ('root', 'sample_delphes_events.root', '_delphes_events.root'),
                                      ('hepmc', 'sample_pythia8_events.hepmc.gz', '_pythia8_events.hepmc.gz')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                result, _, _, work = self.production(Path(tmp), 'condor', outputs, missing=filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('WARNING:', result.stderr)
                with tarfile.open(work / 'output_sample_123_0.tar.gz') as archive:
                    names = archive.getnames()
                self.assertFalse(any(name.endswith(suffix) for name in names))
                for other in ('_delphes_events.root', '_unweighted_events.lhe.gz',
                              '_pythia8_events.hepmc.gz', '_logs.tar'):
                    if other != suffix:
                        self.assertTrue(any(name.endswith(other) for name in names), other)

    def test_output_preparation_failures_preserve_independent_formats(self):
        cases = [('mv', '_delphes_events.root'), ('mv', '_unweighted_events.lhe.gz'),
                 ('mv', '_pythia8_events.hepmc.gz'), ('tar', '-cvf'), ('find', '*.log'),
                 ('gzip', '-c')]
        for failure in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                result, _, _, work = self.production(
                    Path(tmp), 'condor', ['root', 'lhe', 'hepmc', 'lhco', 'log'],
                    decayed=True, output_failure=failure)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('WARNING:', result.stderr)
                with tarfile.open(work / 'output_sample_123_0.tar.gz') as archive:
                    names = archive.getnames()
                self.assertTrue(any(name.endswith('_delphes_events.root') for name in names))
                self.assertTrue(any(name.endswith('_pythia8_events.hepmc.gz') for name in names))
                self.assertTrue(any(name.endswith('_delphes_events.lhco') for name in names))
                self.assertTrue(any(name.endswith('unweighted_events.lhe.gz') for name in names))
                if failure[0] in ('tar', 'find'):
                    self.assertFalse(any(name.endswith('_logs.tar') for name in names))
                self.assertFalse(any(name.endswith('.pending') for name in names))

    def test_final_archive_failure_is_still_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _, _ = self.production(Path(tmp), 'condor', ['lhe'], output_failure=('tar', '-cvzf'))
            self.assertNotEqual(result.returncode, 0)
