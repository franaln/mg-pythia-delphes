"""Merge orchestration regressions; container physics tools are mocked."""
import gzip
import io
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from mgpd.merge import merge_outputs, build_merge_command


class MergingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def archive(self, name, files):
        path = self.base / name
        with tarfile.open(path, 'w:gz') as archive:
            for filename, data in files.items():
                info = tarfile.TarInfo(filename)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def fake_run(self, command, check):
        self.assertTrue(check)
        mount = command[command.index('-v') + 1] if '-v' in command else command[command.index('--bind') + 1]
        work = Path(mount[:-len(':/work')])
        args = shlex.split(command[-1].split(' && ')[-1])
        if args[0].endswith('merge.pl'):
            target = args[-2]
            data = gzip.compress(b'<LesHouchesEvents>merged</LesHouchesEvents>')
        elif args[0] in ('hadd', 'lhco2root'):
            target, data = args[1], b'root merged'
        else:
            self.assertEqual(args[0], 'root2lhco')
            target, data = args[2], b'lhco merged'
        (work / target).write_bytes(data)
        return subprocess.CompletedProcess(command, 0)

    def test_full_merge_and_extract_preserves_originals(self):
        inputs = [self.archive(f'{i}.tar.gz', {
            f'job{i}_unweighted_events.lhe.gz': gzip.compress(b'lhe'),
            f'job{i}_delphes_events.root': b'root',
            f'job{i}_delphes_events.lhco': b'lhco'}) for i in range(2)]
        originals = [p.read_bytes() for p in inputs]
        with patch('mgpd.merge.subprocess.run', side_effect=self.fake_run) as run:
            merge_outputs(inputs, self.base / 'result', backend='local-apptainer',
                          image='/images/test.sif', extract_lhe=True, keep_all=True)
        self.assertEqual(run.call_count, 3)
        self.assertTrue((self.base / 'result/merged_unweighted_events.lhe').is_file())
        self.assertEqual(len(list((self.base / 'result/all').rglob('*.lhe'))), 2)
        self.assertEqual(originals, [p.read_bytes() for p in inputs])

    def test_lhco_only_converts_through_temporary_root(self):
        source = self.archive('a.tar.gz', {'job_delphes_events.lhco': b'lhco'})
        with patch('mgpd.merge.subprocess.run', side_effect=self.fake_run) as run:
            merge_outputs([source], self.base / 'result.tar.gz', backend='local-docker')
        self.assertEqual(run.call_count, 2)
        with tarfile.open(self.base / 'result.tar.gz') as result:
            self.assertEqual(result.getnames(), ['merged_delphes_events.lhco'])

    def test_failure_does_not_publish_output_or_leave_workspace(self):
        source = self.archive('a.tar.gz', {'job_delphes_events.root': b'root'})
        with patch('mgpd.merge.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'hadd')):
            with self.assertRaises(subprocess.CalledProcessError):
                merge_outputs([source], self.base / 'result')
        self.assertFalse((self.base / 'result').exists())
        self.assertFalse(list(self.base.glob('.mgpd-merge-*')))

    def test_missing_command_output_is_failure(self):
        source = self.archive('a.tar.gz', {'job_delphes_events.root': b'root'})
        with patch('mgpd.merge.subprocess.run'):
            with self.assertRaisesRegex(ValueError, 'no output'):
                merge_outputs([source], self.base / 'result')

    def test_rejects_traversal_links_duplicates_and_partial_formats(self):
        unsafe = self.archive('unsafe.tar.gz', {'../escape': b'bad'})
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            merge_outputs([unsafe], self.base / 'result')
        link = self.base / 'link.tar.gz'
        with tarfile.open(link, 'w:gz') as archive:
            member = tarfile.TarInfo('link'); member.type = tarfile.SYMTYPE; member.linkname = '/tmp'
            archive.addfile(member)
        with self.assertRaisesRegex(ValueError, 'Unsupported archive'):
            merge_outputs([link], self.base / 'result')
        a = self.archive('a.tar.gz', {'same_delphes_events.root': b'root'})
        b = self.archive('b.tar.gz', {'same_delphes_events.root': b'root'})
        with self.assertRaisesRegex(ValueError, 'Duplicate job'):
            merge_outputs([a, b], self.base / 'result')
        with self.assertRaisesRegex(ValueError, 'duplicate paths'):
            merge_outputs([a, a], self.base / 'result')
        c = self.archive('c.tar.gz', {'other_delphes_events.lhco': b'lhco'})
        with self.assertRaisesRegex(ValueError, 'missing'):
            merge_outputs([a, c], self.base / 'result')

    def test_destination_never_overwritten(self):
        source = self.archive('a.tar.gz', {'job_delphes_events.root': b'root'})
        output = self.base / 'existing'; output.write_text('keep')
        with self.assertRaises(FileExistsError):
            merge_outputs([source], output)
        self.assertEqual(output.read_text(), 'keep')

    def test_backend_arguments_and_quoted_paths(self):
        source = self.archive('a.tar.gz', {'job with spaces_delphes_events.root': b'root'})
        with patch('mgpd.merge.subprocess.run', side_effect=self.fake_run) as run:
            merge_outputs([source], self.base / 'result', backend='local-docker')
        self.assertIn("'all/00000/job with spaces_delphes_events.root'", run.call_args.args[0][-1])
        with self.assertRaises(ValueError):
            build_merge_command('/tmp', 'true', 'condor', 'image')

    def test_merge_command_help(self):
        result = subprocess.run([sys.executable, '-m', 'mgpd', 'merge', '--help'],
                                cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)

    def test_cli_dispatch_and_error(self):
        from mgpd.cli import main as cli_main
        with patch('mgpd.cli.merge_outputs', return_value='out') as merge:
            self.assertEqual(cli_main(['merge', 'in.tar.gz', '-o', 'out', '--backend', 'local-apptainer']), 0)
            self.assertEqual(merge.call_args.kwargs['backend'], 'local-apptainer')
        with patch('mgpd.cli.merge_outputs', side_effect=ValueError('bad input')):
            self.assertEqual(cli_main(['merge', 'in.tar.gz', '-o', 'out']), 1)
