"""Image lifecycle orchestration: fake engines, real staging/checksum operations."""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/create_images.py'
spec = importlib.util.spec_from_file_location('image_builder', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
IMAGE_ID = 'sha256:' + 'a' * 64


class ImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'output'
        self.install = self.root / 'shared'; self.install.mkdir()
        self.calls = []
        self.context = self.root / 'context'
        (self.context / 'images').mkdir(parents=True)
        for version in builder.VERSIONS:
            (self.context / 'images' / f'mg-pythia-delphes-{version}.Dockerfile').write_text('FROM ubuntu:20.04')
        helper = SCRIPT.parents[1] / 'images/sources.py'
        shutil.copy2(helper, self.context / 'images/sources.py')
        spec = importlib.util.spec_from_file_location('test_source_catalog', helper)
        sources = importlib.util.module_from_spec(spec); spec.loader.exec_module(sources)
        entries = {}
        for version in builder.VERSIONS:
            entries.update({name: {'url': url, 'sha256': 'a'*64} for name, url in sources.catalog(version).items()})
        (self.context / 'images/sources.lock.json').write_text(json.dumps({'schema_version': 1, 'files': entries}))

    def fake_run(self, command, cwd=None, capture=False):
        command = [str(item) for item in command]
        self.calls.append(command)
        if 'inspect' in command:
            return IMAGE_ID + '\n'
        if 'save' in command:
            Path(command[command.index('--output') + 1]).write_bytes(b'archive')
        if command[:2] == ['apptainer', 'build']:
            Path(command[2]).write_bytes(b'SIF')
        return 'checks passed\n' if capture else None

    def args(self, *extra):
        args = builder.parser().parse_args(['--version', '3.5.6', '--source', str(self.context), '--output-dir', str(self.output), *extra])
        builder.validate(args)
        return args

    def execute(self, *extra):
        with patch.object(builder, 'run', side_effect=self.fake_run), \
                patch.object(builder.shutil, 'which', return_value='/fake/tool'), \
                patch.object(builder.subprocess, 'run'):
            builder.process(self.args(*extra), '3.5.6')

    def test_build_check_convert_and_install_without_registry(self):
        self.execute('--build', '--sif', '--check', '--install-dir', str(self.install))
        saves = [cmd for cmd in self.calls if 'save' in cmd]
        self.assertIn('docker-archive', saves[0])
        self.assertEqual(saves[0][-1], IMAGE_ID)
        checks = [cmd for cmd in self.calls if 'exec' in cmd or 'run' in cmd]
        self.assertEqual(len(checks), 2)
        self.assertFalse(any('push' in cmd for cmd in self.calls))
        sif = self.install / 'mg-pythia-delphes-3.5.6.sif'
        self.assertEqual(sif.read_bytes(), b'SIF')
        data = json.loads(sif.with_name(sif.name + '.json').read_text())
        self.assertEqual(data['sif_checked_sha256'], builder.sha256(sif))
        self.assertEqual(data['sif_image_id'], IMAGE_ID)
        self.assertFalse(list(self.output.glob('.mgpd-image-*')))

    def test_podman_build_preserves_dockerfile_shell_and_docker_gets_no_format_flag(self):
        for engine in ('podman', 'docker'):
            self.calls.clear()
            with self.subTest(engine=engine):
                self.execute('--build', '--engine', engine)
                command = self.calls[0]
                if engine == 'podman':
                    self.assertEqual(command[command.index('--format') + 1], 'docker')
                else:
                    self.assertNotIn('--format', command)

    def test_install_only_uses_checked_bytes_without_engine_or_apptainer(self):
        self.execute('--sif', '--check')
        self.calls.clear()
        with patch.object(builder.shutil, 'which', side_effect=AssertionError('Install must not require tools')):
            builder.process(self.args('--install-dir', str(self.install)), '3.5.6')
        self.assertEqual(self.calls, [])
        self.assertTrue((self.install / 'mg-pythia-delphes-3.5.6.sif').is_file())

    def test_build_failure_cannot_tag_push_or_install_old_image(self):
        def fail(command, **kwargs):
            self.calls.append(command)
            raise subprocess.CalledProcessError(1, command)
        with patch.object(builder, 'run', side_effect=fail), \
                patch.object(builder.shutil, 'which', return_value='/fake/tool'), \
                patch.object(builder.subprocess, 'run') as cleanup:
            with self.assertRaises(subprocess.CalledProcessError):
                builder.process(self.args('--build', '--check', '--push'), '3.5.6')
            cleanup.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 'build')
        self.assertEqual(list(self.install.iterdir()), [])

    def test_failed_sif_check_does_not_replace_existing_sif(self):
        self.output.mkdir()
        sif = self.output / 'mg-pythia-delphes-3.5.6.sif'; sif.write_bytes(b'previous')
        def fail(command, **kwargs):
            if command[:2] == ['apptainer', 'exec']:
                raise subprocess.CalledProcessError(1, command)
            return self.fake_run(command, **kwargs)
        with patch.object(builder, 'run', side_effect=fail), \
                patch.object(builder.shutil, 'which', return_value='/fake/tool'):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.process(self.args('--sif', '--check', '--replace'), '3.5.6')
        self.assertEqual(sif.read_bytes(), b'previous')

    def test_unchecked_tampered_or_wrong_image_sif_is_refused(self):
        self.execute('--sif')
        with self.assertRaisesRegex(ValueError, 'checked'):
            builder.process(self.args('--install-dir', str(self.install)), '3.5.6')
        self.execute('--sif', '--check', '--replace')
        sif = self.output / 'mg-pythia-delphes-3.5.6.sif'
        metadata = sif.with_name(sif.name + '.json')
        data = json.loads(metadata.read_text()); data['sif_image_id'] = 'other'
        metadata.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'another image'):
            self.execute('--check')
        sif.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checked'):
            builder.process(self.args('--install-dir', str(self.install)), '3.5.6')

    def test_push_checks_and_uses_exact_id(self):
        self.execute('--check', '--push')
        self.assertEqual(self.calls[-2], ['podman', 'tag', IMAGE_ID, 'franaln/mg-pythia-delphes:3.5.6'])
        self.assertEqual(self.calls[-1], ['podman', 'push', 'franaln/mg-pythia-delphes:3.5.6'])

    def test_docker_conversion_and_revision(self):
        self.execute('--engine', 'docker', '--sif', '--revision', 'r1')
        save = next(cmd for cmd in self.calls if 'save' in cmd)
        self.assertNotIn('--format', save)
        self.assertEqual(save[0], 'docker')
        self.assertTrue((self.output / 'mg-pythia-delphes-3.5.6-r1.sif').is_file())

    def test_invalid_actions_dry_run_and_existing_destinations(self):
        for options in ([], ['--push'], ['--no-cache', '--check'], ['--build', '--revision', '../bad']):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.args(*options)
        with self.assertRaises(SystemExit):
            builder.parser().parse_args(['--version', 'latest', '--build'])
        with patch.object(builder, 'run', side_effect=AssertionError('Dry run must not execute')):
            builder.process(self.args('--build', '--sif', '--check', '--dry-run'), '3.5.6')
        self.assertFalse(self.output.exists())
        self.execute('--sif', '--check')
        with self.assertRaises(FileExistsError):
            self.args('--sif')
        self.execute('--install-dir', str(self.install))
        with self.assertRaises(FileExistsError):
            self.args('--install-dir', str(self.install))

    def test_real_cli_with_fake_executables_from_another_directory(self):
        tools = self.root / 'tools'; tools.mkdir()
        log = self.root / 'commands.jsonl'
        code = f'#!{sys.executable}\n' + """import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['IMAGE_TEST_LOG']).open('a') as stream:
    stream.write(json.dumps([Path(sys.argv[0]).name, *args])+'\\n')
if args and args[0]=='build' and Path(sys.argv[0]).name=='podman':
    if '--format' not in args or args[args.index('--format')+1]!='docker':
        raise SystemExit(33)
if 'inspect' in args:
    print('sha256:'+'a'*64)
elif 'save' in args:
    Path(args[args.index('--output')+1]).write_bytes(b'archive')
elif Path(sys.argv[0]).name=='apptainer' and args[0]=='build':
    Path(args[1]).write_bytes(b'SIF')
"""
        for name in ('podman', 'apptainer'):
            executable = tools / name; executable.write_text(code); executable.chmod(0o755)
        command = [sys.executable, str(SCRIPT), '--version', '3.5.6', '--build', '--sif',
                   '--check', '--push', '--source', str(self.context), '--output-dir', str(self.output)]
        result = subprocess.run(command, cwd=self.root,
                                env={**os.environ, 'PATH': str(tools)+os.pathsep+os.environ['PATH'],
                                     'IMAGE_TEST_LOG': str(log)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        push = next(index for index, cmd in enumerate(commands) if cmd[1]=='push')
        checks = [index for index, cmd in enumerate(commands) if cmd[1] in ('run', 'exec')]
        self.assertTrue(all(index < push for index in checks))
        self.assertEqual(len(checks), 2)
        self.assertTrue((self.output / 'mg-pythia-delphes-3.5.6.sif').is_file())

    def test_checked_recipe_requires_lock_and_passes_build_jobs(self):
        lock = self.root / 'sources.lock.json'
        lock.write_bytes((self.context / 'images/sources.lock.json').read_bytes())
        # The source lock must live inside the selected build context.
        with self.assertRaises(ValueError):
            self.args('--build', '--source-lock', str(lock))
        context = self.root / 'context'
        (context / 'images').mkdir(parents=True, exist_ok=True)
        (context / 'images/mg-pythia-delphes-3.5.6.Dockerfile').write_text('FROM ubuntu:20.04')
        (context / 'sources.lock.json').write_bytes(lock.read_bytes())
        self.execute('--build', '--source', str(context),
                     '--source-lock', 'sources.lock.json', '--build-jobs', '2')
        command = next(cmd for cmd in self.calls if cmd[1] == 'build')
        self.assertIn(str((context / 'images/mg-pythia-delphes-3.5.6.Dockerfile').resolve()), command)
        self.assertIn('BUILD_JOBS=2', command)
        self.assertIn('SOURCE_LOCK=sources.lock.json', command)

    def test_source_lock_mismatch_fails_before_any_engine_command(self):
        lock = self.context / 'images/sources.lock.json'
        data = json.loads(lock.read_text())
        data['files']['pythia8306.tgz']['url'] = 'https://another.example/pythia8306.tgz'
        lock.write_text(json.dumps(data))
        with patch.object(builder, 'run', side_effect=AssertionError('Build must not start')):
            with self.assertRaisesRegex(ValueError, 'URL mismatch for pythia8306.tgz'):
                self.args('--build')

    def test_publication_never_overwrites_or_follows_symlinks(self):
        source = self.root / 'source'; source.write_bytes(b'new')
        target = self.root / 'target'; target.write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            builder.publish_file(source, target)
        self.assertEqual(target.read_bytes(), b'old')
        link = self.root / 'link'; link.symlink_to(target)
        with self.assertRaises(ValueError):
            builder.publish_file(source, link, replace=True)
        with self.assertRaisesRegex(ValueError, 'changed during copying'):
            builder.publish_file(source, target, replace=True, expected_sha256='wrong')
        self.assertEqual(target.read_bytes(), b'old')
        builder.publish_file(source, target, replace=True)
        self.assertEqual(target.read_bytes(), b'new')
