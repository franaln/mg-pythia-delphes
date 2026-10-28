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
from mgpd.results import result_files


class OutputContractTests(unittest.TestCase):
    def production(self, root, mode, outputs, *, decayed=False, missing=None, converter='ok'):
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
            for relative in (f'run_{job["sample"]}.tar.gz', job['run_file'], 'run_mg_pythia_delphes.sh'):
                source = Path(loaded.output_dir) / relative
                shutil.copyfile(source, work / source.name)
            command = ['bash', str(work / 'run_mg_pythia_delphes.sh'), job['sample'],
                       f'run_{job["sample"]}.tar.gz', ' '.join(job['outputs']),
                       'output_sample_123_0', Path(job['run_file']).name]
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
            return result, loaded, job, work
        # Bypass only the container boundary. Run the generated wrapper via the
        # actual saved-plan executor, including checksum and attempt guards.
        def native(mode, image, sample, directory):
            return ['bash', '-c', 'cd ' + shlex.quote(directory) + ' && exec bash ./run_mg_pythia_delphes.sh ' + shlex.quote(sample) + ' unused']
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

    def test_lhco_converter_failure_cannot_be_reported_as_success(self):
        for mode in ('local-apptainer', 'local-docker', 'condor'):
            for converter in ('absent', 'fail', 'silent', 'empty', 'partial'):
                with self.subTest(mode=mode, converter=converter), tempfile.TemporaryDirectory() as tmp:
                    result, prepared, _, _ = self.production(Path(tmp), mode, ['lhco'], converter=converter)
                    self.assertIsNotNone(result)
                    self.assertNotEqual(result.returncode, 0)
                    if mode != 'condor':
                        self.assertEqual(mgpd.read_production_manifest(prepared.output_dir)['state'], 'failed')

    def test_missing_native_outputs_fail_condor_packaging(self):
        for kind, filename in [('lhe', 'unweighted_events.lhe.gz'),
                               ('root', 'sample_delphes_events.root'),
                               ('hepmc', 'sample_pythia8_events.hepmc.gz')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                result, _, _, _ = self.production(Path(tmp), 'condor', [kind], missing=filename)
                self.assertNotEqual(result.returncode, 0)
