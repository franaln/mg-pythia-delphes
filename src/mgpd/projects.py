"""Self-contained editable project templates and project-relative preparation."""
import copy
from pathlib import Path
import re
import shutil
import tempfile

import yaml
from .config import resolve_run_mode, validate_config
from .preparation import prepare_production
from .templates import load_template, _resource



class _Dumper(yaml.SafeDumper):
    pass

def _string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)

_Dumper.add_representer(str, _string)


def create_project(directory, *, template, name=None, level='parton', mode='local', image=None,
                   nevents=100, njobs=1, seed=None):
    """Copy an SM starter YAML/cards into a NEW directory, without running work.

    All cards are editable copies. The bundled param card assumes model sm;
    a different model/process requires corresponding cards. Images are examples,
    not downloaded or checked for runtime availability during creation.
    """

    if template != 'proc':
        raise ValueError(f'Unknown template {template!r}; choose proc')
    if level not in ('parton', 'particle', 'detector'):
        raise ValueError(f'Unknown level {level!r}; choose parton, particle or detector')

    raw = Path(directory).expanduser().absolute()
    if raw.exists() or raw.is_symlink():
        raise FileExistsError(f'Project directory already exists: {raw}')

    destination = raw.resolve()
    if not destination.parent.is_dir():
        raise FileNotFoundError('Project parent directory must exist')
    if any((ancestor / 'manifest.json').exists() for ancestor in destination.parents):
        raise ValueError('Cannot create projects inside prepared productions')

    ######
    if name is None:
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', destination.name)
        if not name or not name[0].isalnum():
            name = 'sample_' + name
    if not isinstance(name, str):
        raise ValueError('Sample name must be a string')

    if image is None:
        resolved = resolve_run_mode(mode)
        image = ('franaln/mg-pythia-delphes:3.5.6' if resolved == 'local-docker' else
                 '/opt/images/mg-pythia-delphes-3.5.6.sif' if resolved == 'local-apptainer' else
                 'mg-pythia-delphes-3.5.6')
    if not isinstance(image, str) or not image.strip():
        raise ValueError('Image must be a nonempty string')

    # Cards
    if level == 'parton':
        cards_dict = {
            'run': 'cards/run_card.dat',
            'param': 'cards/param_card.dat',
        }
        output_list = ['lhe', 'log']
    elif level == 'particle':
        cards_dict = {
            'run': 'cards/run_card.dat',
            'param': 'cards/param_card.dat',
            'pythia': 'cards/pythia8_card.dat',
        }
        output_list = ['lhe', 'log', 'hepmc']
    elif level == 'detector':
        cards_dict = {
            'run': 'cards/run_card.dat',
            'param': 'cards/param_card.dat',
            'pythia': 'cards/pythia8_card.dat',
            'delphes': 'cards/delphes_card_ATLAS.dat',
        }
        output_list = ['lhe', 'log', 'root', 'lhco']

    config = yaml.safe_load(load_template('config_proc.yml'))
    config['run'].update(name=name, mode=mode, image=image, nevents=nevents,
                         njobs=njobs, outputs=output_list)
    config['cards'] = cards_dict
    config['options']['seed'] = 'RANDOM' if seed is None else seed

    header = (f'# Created from mgpd template; review physics settings before running.\n'
              '# mgpd run resolves these card paths from this directory.\n')

    content = header + yaml.dump(config, Dumper=_Dumper, sort_keys=False, allow_unicode=True)

    with tempfile.TemporaryDirectory(prefix='.mgpd-create-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'project'
        (staged / 'cards').mkdir(parents=True)
        for relative in config['cards'].values():
            (staged / relative).write_bytes(_resource(relative).read_bytes())

        (staged / 'config.yml').write_text(content, encoding='utf-8')

        validate_config(config, base_directory=staged)

        destination.mkdir()  # Exclusive reservation; no overwrite, even for empty directories.
        try:
            for child in staged.iterdir():
                shutil.move(str(child), str(destination / child.name))
        except BaseException:
            shutil.rmtree(destination)
            raise

    return {
        'template': template,
        'directory': str(destination),
        'config_path': str(destination / 'config.yml'),
        # 'config': config,
        # 'files': ['config.yml', *sorted(config['cards'].values())]
    }


def project_config_path(path):
    """Resolve a YAML file or a directory containing one default config filename."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        candidates = [path / name for name in ('config.yml', 'config.yaml') if (path / name).is_file()]
        if len(candidates) != 1:
            raise ValueError('Project directory must contain exactly one config.yml or config.yaml')
        path = candidates[0]
    if not path.is_file():
        raise FileNotFoundError(f'Configuration does not exist: {path}')
    return path


def load_project_config(path, mode=None, base_directory=None):
    """Validate and resolve known input paths from the configuration's directory.

    Does not change cwd or rewrite paths inside arbitrary MadGraph commands.
    The legacy load_config API keeps its existing cwd-relative semantics.
    """

    source = project_config_path(path)
    base = Path(base_directory).expanduser().resolve(strict=True) if base_directory is not None else source.parent
    if not base.is_dir():
        raise ValueError('Input base must be a directory')

    config = yaml.safe_load(source.read_text(encoding='utf-8'))
    if mode is not None and isinstance(config, dict) and isinstance(config.get('run'), dict):
        config['run']['mode'] = mode

    validate_config(config, base_directory=base)

    config = copy.deepcopy(config)
    def absolute(value):
        return str((base / value).resolve())
    for key in ('cards', 'input_dirs'):
        if key in config:
            config[key] = {name: ({sample: absolute(card) for sample, card in value.items()}
                                  if isinstance(value, dict) else absolute(value))
                           for name, value in config[key].items()}
    if 'input_dir' in config:
        config['input_dir'] = absolute(config['input_dir'])
    if 'input_files' in config:
        config['input_files'] = [absolute(value) for value in config['input_files']]
    return config


def prepare_project(path, output=None, *, mode=None, force=False, base_directory=None):
    """Prepare a project without running engines or submitting jobs."""
    source = project_config_path(path)
    config = load_project_config(source, mode, base_directory)
    destination = Path(output).expanduser().resolve() if output is not None else source.parent / 'production'

    return prepare_production(config, destination, force=force)
