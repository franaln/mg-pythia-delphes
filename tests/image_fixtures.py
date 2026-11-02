"""Synthetic installed images for tests of workflow behavior, not image resolution."""
from pathlib import Path
import tempfile
from unittest.mock import patch

from mgpd.images import pin_image

IMAGE_ID = 'sha256:' + 'a' * 64


def installed_image(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    image = Path(temporary.name).resolve() / 'fixture-r1.sif'
    image.write_bytes(b'synthetic SIF for workflow tests')

    def pin(reference, mode):
        if mode == 'local-docker':
            return IMAGE_ID, {'kind': 'docker', 'requested': reference,
                              'reference': IMAGE_ID, 'image_id': IMAGE_ID}
        path, identity = pin_image(str(image), mode)
        identity['requested'] = reference
        return path, identity

    mock = patch('mgpd.preparation.pin_image', side_effect=pin)
    mock.start()
    test.addCleanup(mock.stop)
