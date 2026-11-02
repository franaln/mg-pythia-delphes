"""Checked recipes: download locks, failure propagation and recipe shell syntax."""
import hashlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHECKED = ROOT / 'images'
spec = importlib.util.spec_from_file_location('checked_sources', CHECKED / 'sources.py')
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


class CheckedImageTests(unittest.TestCase):
    def archive(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('file'); info.size = 4
            archive.addfile(info, io.BytesIO(b'data'))
        return stream.getvalue()

    def test_verified_download_rejects_mismatch_and_html_without_publishing(self):
        payload = self.archive()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.tar.gz'
            def response(data):
                result = io.BytesIO(data)
                result.geturl = lambda: 'https://example.org/source.tar.gz'
                return result
            with patch.object(sources, 'urlopen', return_value=response(payload)):
                with self.assertRaises(ValueError):
                    sources.download('https://example.org/source.tar.gz', path, '0' * 64)
            self.assertFalse(path.exists())
            with patch.object(sources, 'urlopen', return_value=response(b'<html>error</html>')):
                with self.assertRaises(tarfile.TarError):
                    sources.download('https://example.org/source.tar.gz', path)
            self.assertFalse(path.exists())
            expected = hashlib.sha256(payload).hexdigest()
            with patch.object(sources, 'urlopen', return_value=response(payload)):
                self.assertEqual(sources.download('https://example.org/source.tar.gz', path, expected), expected)
            with patch.object(sources, 'urlopen', side_effect=AssertionError('Verified cache needs no network')):
                self.assertEqual(sources.download('https://example.org/source.tar.gz', path, expected), expected)
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                sources.download('https://example.org/source.tar.gz', path, expected)
            self.assertFalse(list(Path(temporary).glob('.source-*')))

    def test_lock_capture_and_fetch_require_exact_url_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); cache = root / 'cache'; cache.mkdir()
            payload = self.archive()
            files = {}
            for version in ('3.3.2', '3.5.6'):
                files.update(sources.catalog(version))
            for filename in files:
                (cache / filename).write_bytes(payload)
            lock = root / 'lock.json'
            self.assertEqual(sources.main(['lock', '--version', '3.3.2', '--version', '3.5.6',
                                          '--cache', str(cache), '--output', str(lock)]), 0)
            entries = json.loads(lock.read_text())['files']
            self.assertEqual(set(entries), set(files))
            name, url = next(iter(files.items()))
            self.assertEqual(sources.fetch(lock, name, url, cache / name), hashlib.sha256(payload).hexdigest())
            with self.assertRaises(ValueError):
                sources.fetch(lock, name, 'https://another.example/file', cache / name)
            self.assertEqual(sources.main(['lock', '--version', '3.3.2', '--cache', str(cache),
                                          '--output', str(lock)]), 1)

    def test_lock_diagnostics_distinguish_missing_url_and_checksum(self):
        filename = 'pythia8306.tgz'
        url = sources.catalog('3.5.6')[filename]
        for entries, message in [({}, 'missing entry'),
                ({filename: {'url': 'https://other.example/file', 'sha256': 'a'*64}}, 'URL mismatch'),
                ({filename: {'url': url, 'sha256': None}}, 'Invalid SHA256')]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                sources.checked_entry(entries, filename, url)
        entries = {name: {'url': address, 'sha256': 'a'*64}
                   for name, address in sources.catalog('3.5.6').items()}
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / 'lock.json'; lock.write_text(json.dumps({'schema_version': 1, 'files': entries}))
            with patch.object(sources, 'urlopen', side_effect=AssertionError('Check is offline')):
                self.assertEqual(sources.main(['check', '--version', '3.5.6', '--lock', str(lock)]), 0)

    def test_pythia_run_expands_to_catalog_url_in_both_recipes(self):
        for version in ('3.3.2', '3.5.6'):
            recipe = (CHECKED / f'mg-pythia-delphes-{version}.Dockerfile').read_text()
            joined = '\n'.join(line for line in recipe.splitlines() if not line.lstrip().startswith('#')).replace('\\\n', '')
            command = next(line[4:] for line in joined.splitlines() if line.startswith('RUN ') and 'fetch-source' in line and 'pythia${PYTHIA_VERSION}.tgz' in line)
            command = re.sub(r'^--mount=\S+\s+', '', command)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = root / 'fetch-source'; fake.write_text('#!/bin/bash\nprintf "%s\\n" "$1" "$2"\nexit 42\n'); fake.chmod(0o755)
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command],
                    env={**os.environ, 'PATH': str(root)+os.pathsep+os.environ['PATH'],
                         'TMP_DIR': str(root/'build'), 'PYTHIA_VERSION': '8306'}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 42, result.stderr)
                url, name = result.stdout.splitlines()
                self.assertEqual(name, 'pythia8306.tgz')
                self.assertEqual(url, sources.catalog(version)[name])

    def test_delphes_url_and_extraction_do_not_depend_on_archive_root_name(self):
        for version in ('3.3.2', '3.5.6'):
            recipe = (CHECKED / f'mg-pythia-delphes-{version}.Dockerfile').read_text()
            joined = '\n'.join(line for line in recipe.splitlines() if not line.lstrip().startswith('#')).replace('\\\n', '')
            command = next(line[4:] for line in joined.splitlines() if line.startswith('RUN ') and 'fetch-source' in line and 'Delphes-${DELPHES_VERSION}.tar.gz' in line)
            command = re.sub(r'^--mount=\S+\s+', '', command)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / 'source.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    data = b'fixture'
                    member = tarfile.TarInfo('delphes-3.5.0/Makefile'); member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
                tools = root / 'tools'; tools.mkdir()
                fetch = tools / 'fetch-source'
                fetch.write_text('#!/bin/bash\ncp "$DELPHES_TEST_ARCHIVE" "$2"\nprintf "%s" "$1" > "$DELPHES_TEST_URL"\n')
                fetch.chmod(0o755)
                make = tools / 'make'
                make.write_text('#!/bin/bash\ntest -f Makefile || exit 1\npwd > build-directory.txt\n')
                make.chmod(0o755)
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command.replace('source /setup_build.sh', 'true')],
                    cwd=root, env={**os.environ, 'PATH': str(tools)+os.pathsep+os.environ['PATH'],
                                   'DELPHES_VERSION': '3.5.0', 'BUILD_JOBS': '2', 'INSTALL_DIR': str(root/'install'),
                                   'DELPHES_TEST_ARCHIVE': str(archive), 'DELPHES_TEST_URL': str(root/'url')},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root/'url').read_text(), sources.catalog(version)['Delphes-3.5.0.tar.gz'])
                self.assertEqual((root/'install/Delphes/Makefile').read_bytes(), b'fixture')
                self.assertEqual(Path((root/'install/Delphes/build-directory.txt').read_text().strip()).resolve(),
                                 (root/'install/Delphes').resolve())

    def test_fetch_reuses_verified_cache_without_network_and_rejects_missing_or_bad_bytes(self):
        payload = self.archive()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); cache = root / 'cache'; cache.mkdir()
            filename = 'source.tar.gz'; url = 'https://example.org/source.tar.gz'
            cached = cache / filename; cached.write_bytes(payload)
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'schema_version': 1, 'files': {
                filename: {'url': url, 'sha256': hashlib.sha256(payload).hexdigest()}}}))
            output = root / 'work/source.tar.gz'
            with patch.object(sources, 'urlopen', side_effect=AssertionError('No network during build')):
                sources.fetch(lock, filename, url, output, cache_dir=cache, offline=True)
                self.assertEqual(output.read_bytes(), payload)
                self.assertEqual(cached.read_bytes(), payload)
                output.unlink()
                cached.write_bytes(b'bad')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    sources.fetch(lock, filename, url, output, cache_dir=cache, offline=True)
                self.assertFalse(output.exists())
                cached.unlink()
                with self.assertRaisesRegex(FileNotFoundError, 'downloads are disabled'):
                    sources.fetch(lock, filename, url, output, cache_dir=cache, offline=True)
                self.assertFalse(output.exists())

    def test_cache_population_uses_existing_lock_and_never_rewrites_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); payload = self.archive()
            filename = 'source.tar.gz'; url = 'https://example.org/source.tar.gz'
            lock = root/'lock.json'; lock.write_text(json.dumps({'schema_version': 1, 'files': {
                filename: {'url': url, 'sha256': hashlib.sha256(payload).hexdigest()}}}))
            original = lock.read_bytes()
            response = io.BytesIO(payload); response.geturl = lambda: url
            with patch.object(sources, 'catalog', return_value={filename: url}),                     patch.object(sources, 'urlopen', return_value=response):
                self.assertEqual(sources.main(['cache', '--version', '3.5.6', '--lock', str(lock),
                                              '--cache', str(root/'cache')]), 0)
            self.assertEqual((root/'cache'/filename).read_bytes(), payload)
            self.assertEqual(lock.read_bytes(), original)

    def test_all_archive_and_pdf_steps_mount_cache_without_copying_it_to_runtime(self):
        for recipe in CHECKED.glob('*.Dockerfile'):
            text = recipe.read_text()
            self.assertIn('FROM scratch AS mgpd_sources', text)
            self.assertIn('COPY ${SOURCE_CACHE}/ /archives/', text)
            joined = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#')).replace('\\\n', '')
            for line in joined.splitlines():
                if line.startswith('RUN ') and ('fetch-source ' in line or 'bash ${INSTALL_DIR}/scripts/download_pdf.sh ' in line):
                    self.assertIn('from=mgpd_sources', line)
                    self.assertIn('target=/opt/mgpd-source-cache,ro ', line)
            final_stage = text.split('FROM ubuntu:20.04', 1)[1]
            self.assertNotIn('COPY --from=mgpd_sources', final_stage)

    def test_catalog_pdf_names_match_recipes_and_do_not_mutate_tools(self):
        original = copy.deepcopy(sources.TOOLS)
        for version in ('3.3.2', '3.5.6'):
            recipe = (CHECKED / f'mg-pythia-delphes-{version}.Dockerfile').read_text()
            names = set(re.findall(r'download_pdf.sh ([A-Za-z0-9_]+)', recipe))
            expected = {'NNPDF23_nlo_as_0119', 'NNPDF23_lo_as_0130_qed',
                        'NNPDF30_nlo_as_0118', 'NNPDF30_nlo_as_0118_hessian', 'PDF4LHC15_nnlo_mc'}
            if version == '3.5.6':
                expected.add('PDF4LHC21_mc')
            self.assertEqual(names, expected)
            catalog = sources.catalog(version)
            self.assertEqual(set(catalog) - set(sources.TOOLS[version]), {name+'.tar.gz' for name in names})
            for name in names:
                self.assertIn(name+'.tar.gz', catalog)
            self.assertEqual(sources.TOOLS, original)

    def test_recipe_copy_paths_exist_and_no_legacy_recipe_paths_remain(self):
        for recipe in CHECKED.glob('*.Dockerfile'):
            content = recipe.read_text()
            self.assertNotIn('dockerfiles/', content)
            for line in content.splitlines():
                if line.startswith('COPY ') and not any(variable in line for variable in ('${SOURCE_LOCK}', '${SOURCE_CACHE}')):
                    source = line.split()[1]
                    self.assertTrue(list(ROOT.glob(source)), source)
            self.assertIn('ARG SOURCE_LOCK=images/sources.lock.json', content)

    def test_recipes_keep_versions_and_all_run_instructions_parse(self):
        for version in ('3.3.2', '3.5.6'):
            new = (CHECKED / f'mg-pythia-delphes-{version}.Dockerfile').read_text()
            versions = lambda text: dict(re.findall(r'^ARG (\w*VERSION)=(.*)$', text, re.M))
            expected = {'MG_VERSION': version, 'HEPMC_VERSION': '02_06_09',
                        'PYTHIA_VERSION': '8306', 'DELPHES_VERSION': '3.5.0',
                        'MG5aMC_PY8_INTERFACE_VERSION': '1.3',
                        'FASTJET_VERSION': '3.3.4' if version == '3.3.2' else '3.4.2',
                        'LHAPDF_VERSION': '6.3.0' if version == '3.3.2' else '6.5.1',
                        'ROOT_VERSION': 'root_v6.24.02.Linux-ubuntu20-x86_64-gcc9.3.tar.gz' if version == '3.3.2'
                                        else 'root_v6.30.06.Linux-ubuntu20.04-x86_64-gcc9.4.tar.gz'}
            self.assertEqual(versions(new), expected)
            self.assertNotRegex(new, r'(?:RUN|&&|;)\s*wget\s')
            self.assertNotIn('--from=503', new)
            self.assertIn('ARG BUILD_JOBS=4', new)
            instructions = '\n'.join(line for line in new.splitlines() if not line.lstrip().startswith('#')).replace('\\\n', '').splitlines()
            for line in instructions:
                if line.startswith('RUN '):
                    with self.subTest(version=version, command=line[:80]):
                        result = subprocess.run(['bash', '-n'], input=line[4:], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('sources.lock.json', new)
            self.assertIn('USER docker', new)

    def test_setup_returns_failure_for_incomplete_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = (CHECKED / 'setup_mg_pythia_delphes.sh').read_text().replace('__INS_DIR__', temporary)
            for suffix in ('', '; echo caller-still-runs'):
                result = subprocess.run(['bash', '-c', script + suffix],
                                        env={k:v for k,v in os.environ.items() if k != 'MG_DIR'},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Incomplete MGPD installation', result.stderr)

    def test_pdf_download_failure_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'share/LHAPDF').mkdir(parents=True)
            tools = root / 'tools'; tools.mkdir()
            fetch = tools / 'fetch-source'; fetch.write_text('#!/bin/bash\nexit 42\n'); fetch.chmod(0o755)
            script = (CHECKED / 'download_pdf.sh').read_text().replace('__INS_DIR__', str(root))
            result = subprocess.run(['bash', '-c', script, 'download_pdf.sh', 'NNPDF23_lo_as_0130_qed'],
                                    env={**os.environ, 'PATH': str(tools)+os.pathsep+os.environ['PATH']},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 42)
