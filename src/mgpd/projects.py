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


def copy_project(source, directory, *, name=None, level=None, mode=None, image=None,
                 nevents=None, njobs=None, seed=None, input_validator=None):
    """Clone editable project files into a new directory; only explicit overrides apply.

    Prepared productions, repository metadata and virtual environments are omitted.
    Referenced external input files/directories are copied and their paths rebased.
    Symlinks and special files are refused rather than followed.
    """

    raw = Path(directory).expanduser().absolute()
    if raw.exists() or raw.is_symlink():
        raise FileExistsError(f'Project directory already exists: {raw}')
    destination = raw.resolve()

    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError('Copy source must be a project directory')

    if destination.is_relative_to(source):
        raise ValueError('Copy destination must be outside the source project')

    if not destination.parent.is_dir():
        raise FileNotFoundError('Project parent directory must exist')

    for path in (source, destination.parent):
        if any((ancestor / 'manifest.json').exists() for ancestor in (path, *path.parents)):
            raise ValueError('Cannot copy projects from or into prepared productions')

    if any((source / name).is_symlink() for name in ('config.yml', 'config.yaml')):
        raise ValueError('Project configuration cannot be a symlink')

    config_path = project_config_path(source)
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Configuration must contain a mapping')
    if input_validator:
        input_validator(config, source)

    validate_config(config, base_directory=source)

    config = copy.deepcopy(config)

    for key, value in (('name', name), ('mode', mode), ('image', image),
                       ('nevents', nevents), ('njobs', njobs)):
        if value is not None:
            config['run'][key] = value

    if image is not None and (not isinstance(image, str) or not image.strip()):
        raise ValueError('Image must be a nonempty string')

    if seed is not None:
        config.setdefault('options', {})['seed'] = seed

    if level is not None and level not in ('parton', 'particle', 'detector'):
        raise ValueError('Unknown level; choose parton, particle or detector')

    if level is not None and any(key in config for key in ('input_dir', 'input_dirs', 'input_files')):
        raise ValueError('Level overrides require an inline-process configuration with cards')

    omitted = {'.git', '.venv', '__pycache__', '.pytest_cache'}
    def copy_tree(src, dst):
        if src.is_symlink():
            raise ValueError(f'Project copies do not follow symlinks: {src}')
        if src.is_dir():
            if (src / 'manifest.json').exists() or (src / 'pyvenv.cfg').exists():
                raise ValueError(f'Referenced input is a production or virtual environment: {src}')
            dst.mkdir()
            for child in src.iterdir():
                if child.name in omitted or child.name.startswith('.mgpd'):
                    continue
                if not child.is_symlink() and child.is_dir() and (
                        (child / 'manifest.json').exists() or (child / 'pyvenv.cfg').exists()):
                    continue
                copy_tree(child, dst / child.name)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            raise ValueError(f'Unsupported project input: {src}')

    with tempfile.TemporaryDirectory(prefix='.mgpd-copy-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'project'

        copy_tree(source, staged)

        external = {}

        def relocate(value):
            path = Path(value).expanduser()
            path = path if path.is_absolute() else source / path
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(source):
                relative = resolved.relative_to(source)
                if not (staged / relative).exists():
                    raise ValueError(f'Referenced input was excluded from the copy: {path}')
            else:
                if resolved not in external:
                    # Use a private namespace without clobbering copied project files.
                    folder = staged / '_mgpd_inputs'
                    if not external:
                        folder.mkdir()
                    relative = Path('_mgpd_inputs') / f'{len(external):04d}' / resolved.name
                    (staged / relative).parent.mkdir()
                    copy_tree(path, staged / relative)
                    external[resolved] = relative
                relative = external[resolved]
            return str(relative)

        for key in ('cards', 'input_dirs'):
            if key in config:
                config[key] = {key: ({sample: relocate(item) for sample, item in value.items()}
                                    if isinstance(value, dict) else relocate(value))
                               for key, value in config[key].items()}
        if 'input_dir' in config:
            config['input_dir'] = relocate(config['input_dir'])
        if 'input_files' in config:
            config['input_files'] = [relocate(value) for value in config['input_files']]
        if level is not None:
            cards = config['cards']
            for key in ('pythia', 'delphes'):
                if level == 'parton' or key == 'delphes' and level == 'particle':
                    cards.pop(key, None)
            for key, filename in (('pythia', 'pythia8_card.dat'), ('delphes', 'delphes_card_ATLAS.dat')):
                if key not in cards and (level == 'detector' or key == 'pythia' and level == 'particle'):
                    folder = staged / '_mgpd_cards'
                    folder.mkdir(exist_ok=True)
                    target = folder / filename
                    if target.exists():
                        raise FileExistsError(f'Bundled card destination already exists: {target}')
                    target.write_bytes(_resource('cards/' + filename).read_bytes())
                    cards[key] = str(target.relative_to(staged))
            config['run']['outputs'] = {'parton': ['lhe', 'log'], 'particle': ['lhe', 'log', 'hepmc'],
                                        'detector': ['lhe', 'log', 'root', 'lhco']}[level]

        validate_config(config, base_directory=staged)

        (staged / config_path.name).write_text(yaml.dump(config, Dumper=_Dumper, sort_keys=False), encoding='utf-8')

        destination.mkdir()
        try:
            for child in staged.iterdir():
                shutil.move(str(child), str(destination / child.name))
        except BaseException:
            shutil.rmtree(destination)
            raise

    return {
        'template': 'copy',
        'directory': str(destination),
        'config_path': str(destination / config_path.name)
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
