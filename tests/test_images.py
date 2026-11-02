"""Image discovery and production identity with synthetic bytes/engine responses."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mgpd.cli import main
from mgpd.config import resolve_container_image
from mgpd.images import pin_image, verify_image, list_sif_images, list_docker_images
from mgpd.preparation import prepare_production
from mgpd.execution import execute_production, load_prepared_production
from mgpd.storage import plan_sha256

IMAGE_ID = 'sha256:' + 'a' * 64
DIGEST = 'sha256:' + 'b' * 64


class ImageIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.revision = self.root / 'mg-pythia-delphes-3.5.6-r1.sif'
        self.revision.write_bytes(b'first revision')
        self.alias = self.root / 'mg-pythia-delphes-3.5.6.sif'
        self.alias.symlink_to(self.revision.name)
        self.card = self.root / 'card.dat'; self.card.write_text('')

    def prepare(self, mode='condor', image=None):
        config = {'run': {'name': 'sample', 'mode': mode, 'image': image or str(self.alias),
                          'nevents': 2, 'outputs': ['lhe']},
                  'cards': {'run': str(self.card)}, 'process': 'generate p p > e+ e-'}
        with redirect_stdout(io.StringIO()):
            return prepare_production(config, self.root / 'production')

    def test_version_and_revision_selectors_for_both_backends(self):
        for mode in ('condor', 'local-apptainer'):
            for name in ('3.5.6-r1', 'mg-pythia-delphes:3.5.6-r1', 'mg-pythia-delphes-3.5.6-r1'):
                self.assertEqual(resolve_container_image({'image': name}, mode),
                                 '/opt/images/mg-pythia-delphes-3.5.6-r1.sif')
        self.assertEqual(resolve_container_image({'image': '3.5.6-r1'}, 'local-docker'),
                         'franaln/mg-pythia-delphes:3.5.6-r1')
        self.assertEqual(resolve_container_image({'image': str(self.revision)}, 'condor'), str(self.revision))

    def test_alias_changes_do_not_change_prepared_condor_or_identity(self):
        production = self.prepare()
        self.assertEqual(production.manifest['image'], str(self.revision))
        self.assertIn(str(self.revision), (Path(production.output_dir) / production.submit_file).read_text())
        second = self.root / 'mg-pythia-delphes-3.5.6-r2.sif'; second.write_bytes(b'second revision')
        self.alias.unlink(); self.alias.symlink_to(second.name)
        loaded = load_prepared_production(production.output_dir)
        self.assertEqual(loaded.manifest['image'], str(self.revision))
        verify_image(loaded.manifest['image'], loaded.manifest['image_identity'])
        changed = dict(loaded.manifest, image_identity={**loaded.manifest['image_identity'], 'sha256': 'wrong'})
        self.assertNotEqual(plan_sha256(changed), loaded.manifest['plan_sha256'])

    def test_changed_sif_blocks_execution_before_attempt_or_submit(self):
        production = self.prepare()
        self.revision.write_bytes(b'replaced')
        with patch('mgpd.execution.subprocess.run') as run, self.assertRaisesRegex(ValueError, 'changed'):
            execute_production(production)
        run.assert_not_called()
        self.assertNotIn('execution_attempt', load_prepared_production(production.output_dir).manifest)

    def test_missing_image_fails_before_output_creation(self):
        with self.assertRaises(FileNotFoundError):
            self.prepare(image=str(self.root / 'missing.sif'))
        self.assertFalse((self.root / 'production').exists())

    def test_docker_digest_is_used_in_prepared_execution(self):
        reference = 'franaln/mg-pythia-delphes:3.5.6'
        pinned = 'franaln/mg-pythia-delphes@' + DIGEST
        response = subprocess.CompletedProcess([], 0, json.dumps([{'Id': IMAGE_ID, 'RepoDigests': [pinned]}]), '')
        with patch('mgpd.images.subprocess.run', return_value=response) as inspect:
            production = self.prepare(mode='local-docker', image=reference)
        inspect.assert_called_once()
        self.assertEqual(inspect.call_args.args[0], ['docker', 'image', 'inspect', reference])
        self.assertEqual(production.manifest['image'], pinned)
        with patch('mgpd.execution.subprocess.run') as run, redirect_stdout(io.StringIO()):
            execute_production(production)
        self.assertIn(pinned, run.call_args.args[0])
        self.assertNotIn(reference, run.call_args.args[0])

    def test_local_docker_image_falls_back_to_content_id_and_missing_tag_fails(self):
        response = subprocess.CompletedProcess([], 0, json.dumps([{'Id': IMAGE_ID, 'RepoDigests': []}]), '')
        with patch('mgpd.images.subprocess.run', return_value=response):
            pinned, identity = pin_image('custom:tag', 'local-docker')
        self.assertEqual(pinned, IMAGE_ID)
        verify_image(pinned, identity)
        with patch('mgpd.images.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'docker')):
            with self.assertRaisesRegex(ValueError, 'docker pull'):
                pin_image('missing:tag', 'local-docker')

    def test_sif_listing_marks_aliases_and_cli_is_read_only(self):
        entries = list_sif_images(self.root)
        self.assertEqual(len(entries), 2)
        recommended = next(item for item in entries if item['recommended'])
        self.assertEqual(recommended['target'], str(self.revision))
        before = sorted(self.root.iterdir())
        output = io.StringIO()
        with redirect_stdout(output), patch('mgpd.images.subprocess.run') as run:
            self.assertEqual(main(['images', '--backend', 'local-apptainer', '--directory', str(self.root), '--json']), 0)
        run.assert_not_called()
        self.assertEqual(len(json.loads(output.getvalue())['images']), 2)
        self.assertEqual(before, sorted(self.root.iterdir()))

    def test_remote_catalog_paginates_and_handles_errors(self):
        first = io.BytesIO(json.dumps({'results': [{'name': '3.5.6', 'digest': DIGEST}],
                                      'next': 'https://hub.docker.com/v2/page2'}).encode())
        second = io.BytesIO(json.dumps({'results': [{'name': '3.5.6-r1'}], 'next': None}).encode())
        with patch('mgpd.images.urlopen', side_effect=[first, second]) as request:
            entries = list_docker_images(remote=True)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(entries[0]['recommended'])
        self.assertFalse(entries[1]['recommended'])
        with patch('mgpd.images.urlopen', side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, 'Cannot query Docker Hub'):
                list_docker_images(remote=True)

    def test_local_docker_listing_does_not_query_hub(self):
        data = {'Repository': 'franaln/mg-pythia-delphes', 'Tag': '3.5.6-r1', 'Digest': DIGEST}
        with patch('mgpd.images.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(data))), \
                patch('mgpd.images.urlopen') as network:
            entries = list_docker_images()
        self.assertEqual(entries[0]['digest'], DIGEST)
        network.assert_not_called()
