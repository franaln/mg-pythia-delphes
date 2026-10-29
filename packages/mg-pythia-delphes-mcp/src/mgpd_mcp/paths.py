"""Workspace checks for read-only adapter operations (not an execution sandbox)."""
import json
from pathlib import Path


class PathError(ValueError):
    pass


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Workspace must be a directory')

    def path(self, value, base=None):
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = (base or self.root) / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise PathError('Requested path leaves the configured workspace')
        return resolved

    def config_inputs(self, config, base):
        values = []
        for key in ('cards', 'input_dirs'):
            mapping = config.get(key, {})
            if not isinstance(mapping, dict):
                raise ValueError(f'{key} must be a mapping')
            values.extend(mapping.values())
        listed = config.get('input_files', [])
        if not isinstance(listed, list):
            raise ValueError('input_files must be a list')
        values.extend(listed)
        if config.get('input_dir'):
            values.append(config['input_dir'])
        for value in values:
            for item in value.values() if isinstance(value, dict) else [value]:
                self.path(item, base)

    def production(self, directory, seen=None):
        """Check mutable output paths and linked manifests before core reads them."""
        seen = set() if seen is None else seen
        directory = self.path(directory)
        if directory in seen:
            return directory
        seen.add(directory)
        manifest = self.path(directory / 'manifest.json')
        metadata = json.loads(manifest.read_text())
        for relative in metadata.get('execution_input_sha256', {}):
            self.path(relative, directory)
        for job in metadata.get('jobs', []):
            if job.get('run_file'):
                run = self.path(job['run_file'], directory).parent / 'RUN'
                run = self.path(run)
                events = self.path(run / 'Events')
                for name in ('run_01', 'run_01_decayed_1'):
                    folder = self.path(events / name)
                    if folder.is_dir():
                        for file in folder.iterdir():
                            self.path(file)
                if run.is_dir():
                    for log in run.rglob('*.log'):
                        self.path(log)
        for pattern in ('output_*.tar.gz', 'job_*.log'):
            for file in directory.glob(pattern):
                self.path(file)
        recovery = metadata.get('recovery')
        if recovery:
            self.production(recovery['parent_directory'], seen)
        for entry in metadata.get('recoveries', []):
            if entry.get('blocks_jobs', True):
                self.production(entry['directory'], seen)
        return directory
