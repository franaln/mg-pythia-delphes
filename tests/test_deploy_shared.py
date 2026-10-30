"""Deployment action boundaries with fake installers; no shared paths changed."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('deploy_shared', Path(__file__).resolve().parents[1] / 'scripts/deploy_shared.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = self.root / 'venvs'
        self.prefix.mkdir()
        self.bins = self.root / 'bin'
        self.old = self.environment('0.1.1')
        self.current = self.prefix / 'mgpd-current'
        self.current.symlink_to(self.old)
        self.wheel = self.make_wheel('mg-pythia-delphes', '0.2.0')
        self.mcp = self.make_wheel('mg-pythia-delphes-mcp', '0.2.0')
        self.commands = []
        self.fail = None
        self.mock = patch.object(deployment.subprocess, 'run', side_effect=self.run_fake)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def make_wheel(self, name, version):
        normalized = name.replace('-', '_')
        path = self.root / f'{normalized}-{version.replace("/", "_")}-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(f'{normalized}.dist-info/METADATA', f'Name: {name}\nVersion: {version}\n')
        return path

    def environment(self, version, mcp=False):
        path = self.prefix / f'mgpd-{version}'
        (path / 'bin').mkdir(parents=True)
        (path / 'bin/python').touch()
        (path / 'bin/mgpd').touch()
        if mcp:
            (path / 'bin/mgpd-mcp').touch()
        return path

    def run_fake(self, command, check):
        self.commands.append(command)
        if len(command) > 2 and command[1] == '-c':
            compile(command[2], '<deployment-check>', 'exec')
        if self.fail and self.fail(command):
            raise subprocess.CalledProcessError(1, command)
        if command[1:3] == ['-m', 'build']:
            destination = Path(command[command.index('--outdir') + 1]); destination.mkdir(parents=True)
            wheel = self.mcp if command[-1].endswith('mg-pythia-delphes-mcp') else self.wheel
            shutil.copyfile(wheel, destination / wheel.name)
        elif command[1:3] == ['-m', 'venv']:
            (Path(command[3]) / 'bin').mkdir()
            (Path(command[3]) / 'bin/python').touch()
        elif command[1:4] == ['-m', 'pip', 'install']:
            bin_dir = Path(command[0]).parent
            (bin_dir / 'mgpd').touch()
            if any('mg_pythia_delphes_mcp-' in str(argument) for argument in command[4:]):
                (bin_dir / 'mgpd-mcp').touch()
        return subprocess.CompletedProcess(command, 0)

    def cli(self, *args):
        with redirect_stderr(self.output):
            return deployment.main(['--prefix', str(self.prefix), *args])

    def test_stage_combined_environment_without_changing_current(self):
        version = deployment.deploy(self.wheel, self.prefix, 'python3.11', mcp_wheel=self.mcp, version='0.2.0')
        self.assertEqual(version, '0.2.0')
        self.assertEqual(self.current.resolve(), self.old)
        environment = self.prefix / 'mgpd-0.2.0'
        self.assertTrue((environment / '.mgpd-install-complete').exists())
        self.assertFalse((environment / '.mgpd-installing').exists())
        self.assertTrue((environment / 'bin/mgpd-mcp').is_file())
        self.assertTrue(any('mgpd_mcp.__version__' in str(command) for command in self.commands))
        self.assertFalse(self.bins.exists())

    def test_build_only_publishes_both_wheels_without_touching_installation(self):
        output = self.root / 'release'
        self.assertEqual(self.cli('--build', '--with-mcp', '--version', '0.2.0',
                                  '--outdir', str(output), '--source', str(self.root)), 0)
        self.assertEqual({path.name for path in output.glob('*.whl')}, {self.wheel.name, self.mcp.name})
        self.assertEqual(self.current.resolve(), self.old)
        self.assertFalse((self.prefix / 'mgpd-0.2.0').exists())
        self.assertTrue(all(command[1:3] == ['-m', 'build'] for command in self.commands))
        self.assertFalse(self.bins.exists())

    def test_core_only_build_and_existing_wheel_refusal(self):
        output = self.root / 'release'
        self.assertEqual(self.cli('--build', '--outdir', str(output)), 0)
        self.assertEqual([path.name for path in output.glob('*.whl')], [self.wheel.name])
        original = (output / self.wheel.name).read_bytes()
        self.assertEqual(self.cli('--build', '--with-mcp', '--outdir', str(output)), 1)
        self.assertEqual((output / self.wheel.name).read_bytes(), original)
        self.assertFalse((output / self.mcp.name).exists())
        self.assertEqual(self.current.resolve(), self.old)

    def test_wrong_build_version_publishes_nothing(self):
        output = self.root / 'release'
        self.assertEqual(self.cli('--build', '--with-mcp', '--version', '0.1.1', '--outdir', str(output)), 1)
        self.assertFalse(list(output.glob('*.whl')))
        self.assertEqual(self.current.resolve(), self.old)

    def test_mismatched_built_pair_publishes_neither_wheel(self):
        self.mcp = self.make_wheel('mg-pythia-delphes-mcp', '0.3.0a1')
        output = self.root / 'release'
        self.assertEqual(self.cli('--build', '--with-mcp', '--outdir', str(output)), 1)
        self.assertFalse(list(output.glob('*.whl')))
        self.assertEqual(self.current.resolve(), self.old)

    def test_build_only_rejects_install_actions_and_unused_arguments(self):
        for arguments in (['--build', '--deploy-venv'], ['--build', '--update-current'],
                          ['--outdir', str(self.root)], ['--update-current', '--version', '0.2.0', '--source', str(self.root)]):
            with self.subTest(arguments=arguments), redirect_stderr(self.output), self.assertRaises(SystemExit):
                deployment.main(arguments)
        self.assertFalse(self.commands)

    def test_source_build_uses_caller_python_and_separate_build_directories(self):
        self.assertEqual(self.cli('--deploy-venv', '--with-mcp', '--version', '0.2.0',
                                  '--python', 'target-venv-python'), 0)
        builds = [command for command in self.commands if command[1:3] == ['-m', 'build']]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(command[0] == sys.executable for command in builds))
        self.assertNotEqual(builds[0][builds[0].index('--outdir') + 1], builds[1][builds[1].index('--outdir') + 1])
        self.assertEqual(self.current.resolve(), self.old)
        self.assertTrue((self.prefix / 'mgpd-0.2.0/bin/mgpd-mcp').is_file())

    def test_combined_install_activate_and_launchers(self):
        self.assertEqual(self.cli('--deploy-venv', '--update-current', '--version', '0.2.0',
                                  '--wheel', str(self.wheel), '--mcp-wheel', str(self.mcp),
                                  '--create-wrapper', '--bin-dir', str(self.bins)), 0)
        self.assertEqual(self.current.resolve(), self.prefix / 'mgpd-0.2.0')
        self.assertTrue((self.bins / 'mgpd').is_file())
        self.assertTrue((self.bins / 'mgpd-mcp').is_file())
        self.assertEqual(sum(command[1:3] == ['-m', 'venv'] for command in self.commands), 1)

    def test_activate_existing_environment_without_installing_or_building(self):
        environment = self.environment('0.2.0', mcp=True)
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0', '--with-mcp'), 0)
        self.assertEqual(self.current.resolve(), environment)
        self.assertFalse(any('venv' in command or 'build' in command or 'install' in command for command in self.commands))
        self.assertIn('Previous target', self.output.getvalue())

    def test_reuse_core_wrapper_and_add_mcp_wrapper(self):
        environment = self.environment('0.2.0', mcp=True)
        self.bins.mkdir()
        core = self.bins / 'mgpd'
        core.write_text(deployment.wrapper_contents(self.prefix, False)['mgpd'])
        core.chmod(0o755)
        original = core.read_bytes()
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0', '--with-mcp',
                                  '--create-wrapper', '--bin-dir', str(self.bins)), 0)
        self.assertEqual(self.current.resolve(), environment)
        self.assertEqual(core.read_bytes(), original)
        self.assertIn('mgpd-current/bin/mgpd-mcp', (self.bins / 'mgpd-mcp').read_text())

    def test_install_failure_keeps_current_and_blocks_activation(self):
        self.fail = lambda command: command[1:4] == ['-m', 'pip', 'install']
        self.assertEqual(self.cli('--deploy-venv', '--wheel', str(self.wheel), '--mcp-wheel', str(self.mcp)), 1)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertTrue((self.prefix / 'mgpd-0.2.0/.mgpd-installing').exists())
        self.fail = None
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0'), 1)
        self.assertEqual(self.current.resolve(), self.old)

    def test_existing_version_never_overwritten(self):
        existing = self.environment('0.2.0', mcp=True)
        sentinel = existing / 'keep'; sentinel.write_text('preserve')
        self.assertEqual(self.cli('--deploy-venv', '--wheel', str(self.wheel), '--mcp-wheel', str(self.mcp)), 1)
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.assertFalse(any('venv' in command for command in self.commands))

    def test_missing_adapter_or_broken_validation_cannot_switch_current(self):
        self.environment('0.2.0')
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0', '--with-mcp'), 1)
        self.assertEqual(self.current.resolve(), self.old)
        self.fail = lambda command: command[1:4] == ['-m', 'pip', 'check']
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0'), 1)
        self.assertEqual(self.current.resolve(), self.old)

    def test_wrapper_collision_rejected_before_switching_current(self):
        self.environment('0.2.0', mcp=True)
        self.bins.mkdir()
        (self.bins / 'mgpd').write_text('unmanaged command')
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0', '--with-mcp',
                                  '--create-wrapper', '--bin-dir', str(self.bins)), 1)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual((self.bins / 'mgpd').read_text(), 'unmanaged command')
        self.assertFalse((self.bins / 'mgpd-mcp').exists())

    def test_wheel_mismatch_and_expected_version_rejected_before_install(self):
        wrong = self.make_wheel('mg-pythia-delphes-mcp', '0.3.0a1')
        self.assertEqual(self.cli('--deploy-venv', '--wheel', str(self.wheel), '--mcp-wheel', str(wrong)), 1)
        self.assertEqual(self.cli('--deploy-venv', '--wheel', str(self.wheel), '--version', '0.3.0'), 1)
        self.assertFalse((self.prefix / 'mgpd-0.2.0').exists())
        self.assertFalse(self.commands)

    def test_unsafe_version_and_non_symlink_current_rejected(self):
        self.assertEqual(self.cli('--update-current', '--version', '../outside'), 1)
        self.environment('0.2.0')
        self.current.unlink(); self.current.mkdir()
        self.assertEqual(self.cli('--update-current', '--version', '0.2.0'), 1)
        self.assertTrue(self.current.is_dir())
        self.assertFalse(self.current.is_symlink())

    def test_ambiguous_cli_actions_and_mixed_sources_are_errors(self):
        for arguments in ([], ['--update-current'], ['--deploy-venv', '--bin-dir', str(self.bins)],
                          ['--version', '0.2.0', '--create-wrapper'],
                          ['--deploy-venv', '--wheel', str(self.wheel), '--with-mcp'],
                          ['--deploy-venv', '--mcp-wheel', str(self.mcp)]):
            with self.subTest(arguments=arguments), redirect_stderr(self.output), self.assertRaises(SystemExit):
                deployment.main(arguments)
        self.assertEqual(self.current.resolve(), self.old)
