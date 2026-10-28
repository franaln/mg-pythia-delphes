"""YAML loading, validation and container-reference resolution."""
import os
import yaml
import re
from pathlib import Path
import copy
from .models import MAX_SEED


def get_config_options(config):
    config_options = []
    if 'options' in config:
        opts = config['options']
        if 'seed' in opts:
            if isinstance(opts['seed'], str) and opts['seed'].strip().upper() == 'RANDOM':
                config_options.append(f'set iseed = RANDOM')
            else:
                config_options.append(f'set iseed = {opts["seed"]}')
        if 'ecm' in opts:
            config_options.append(f'set ebeam1 = {float(opts["ecm"]) / 2}')
            config_options.append(f'set ebeam2 = {float(opts["ecm"]) / 2}')
        if 'use_syst' in opts:
            config_options.append(f'set use_syst = {opts["use_syst"]}')
        if 'extra' in opts:
            if isinstance(opts['extra'], str):
                config_options.append(opts['extra'])
            elif isinstance(opts['extra'], list):
                config_options.extend(opts['extra'])
        if 'user_cuts' in opts:
            config_options.append('set user_cut_module user_modules.py')

    return config_options


def get_expert_options(config):
    config_options = []
    if 'expert' in config:
        opts = config['expert']
        if 'mode' in opts:
            if opts["mode"] == 'single':
                config_options.append('set run_mode 0')
            elif opts['mode'] == 'multi':
                config_options.append('set run_mode 2')
        if 'ncores' in opts:
            ncores = 'None' if str(opts['ncores']).lower() == 'all' else opts['ncores']
            config_options.append(f'set nb_core {ncores}')

    return config_options


def validate_config(config):
    """Validate inputs without creating directories or submitting work.

    Relative input paths retain the CLI's historical current-directory semantics.
    """
    if not isinstance(config, dict) or not isinstance(config.get('run'), dict):
        raise ValueError("Configuration must contain a 'run' mapping")
    run = config['run']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(run.get('name', ''))):
        raise ValueError('run.name must be a nonempty filesystem-safe name')
    for key in ('nevents', 'njobs'):
        value = run.get(key, 10000 if key == 'nevents' else 1)
        if type(value) is not int or value <= 0:
            raise ValueError(f'run.{key} must be a positive integer')
    modes = ('local-docker', 'local-apptainer', 'condor', 'jupiter')
    if run.get('mode', 'local-docker') not in modes:
        raise ValueError(f'run.mode must be one of {modes}')
    outputs = run.get('outputs', ['lhe', 'lhco', 'log'])
    if not isinstance(outputs, list) or not outputs or any(
        x not in ('lhe', 'lhco', 'root', 'hepmc', 'hepmc0', 'log', 'all') for x in outputs
    ):
        raise ValueError('run.outputs must be a nonempty list of supported outputs')
    options = config.get('options', {})
    if not isinstance(options, dict):
        raise ValueError('options must be a mapping')
    seed = options.get('seed')
    if not (seed is None or type(seed) is int and 0 <= seed <= MAX_SEED
            or isinstance(seed, str) and seed.strip().upper() == 'RANDOM'):
        raise ValueError(f'options.seed must be 0, RANDOM, or an integer from 1 to {MAX_SEED}')
    samples = len(config.get('input_dirs', {})) if 'input_dirs' in config else 1
    if isinstance(config.get('cards', {}).get('param'), dict):
        samples = len(config['cards']['param'])
    if not 1 <= samples * run.get('njobs', 1) <= MAX_SEED:
        raise ValueError(f'Production must contain between 1 and {MAX_SEED} jobs')
    paths = []
    if 'input_dirs' in config:
        if not isinstance(config['input_dirs'], dict) or not config['input_dirs']:
            raise ValueError('input_dirs must be a nonempty name-to-directory mapping')
        for name, directory in config['input_dirs'].items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(name)):
                raise ValueError('input_dirs names must be filesystem-safe')
            paths.append(Path(directory) / 'run.mg5')
    elif 'input_dir' in config:
        paths.append(Path(config['input_dir']) / 'run.mg5')
    elif 'input_files' in config:
        paths.extend(Path(x) for x in config['input_files'])
        if not any(x.name == 'run.mg5' for x in paths):
            raise ValueError('input_files must include run.mg5')
    else:
        cards = config.get('cards')
        if not isinstance(cards, dict) or 'run' not in cards:
            raise ValueError('cards.run is required')
        if 'proc' not in cards and not config.get('process'):
            raise ValueError('process or cards.proc is required')
        for card in cards.values():
            paths.extend(Path(x) for x in (card.values() if isinstance(card, dict) else [card]))
        if any(x in outputs for x in ('root', 'lhco')) and 'delphes' not in cards:
            raise ValueError('ROOT/LHCO output requires a Delphes card')
    for path in paths:
        if not path.is_file():
            raise ValueError(f'Input file does not exist: {path}')
    return config


def load_config(config_file, run_mode=None):
    """Load and validate YAML. Relative inputs retain current-directory semantics."""
    with open(config_file) as source:
        config = yaml.safe_load(source)
    if run_mode is not None and isinstance(config, dict) and isinstance(config.get('run'), dict):
        config['run']['mode'] = run_mode
    return validate_config(config)


def resolve_container_image(config_run, run_mode):
    """Resolve a configured image without modifying the caller's configuration."""
    config_run = copy.deepcopy(config_run)
    available_images = [
        'mg-pythia-delphes-3.3.2',
        'mg-pythia-delphes-3.5.6',
        'mg-pythia-delphes-latest',
    ]

    ## Docker/Apptainer image
    if run_mode in ('condor', 'jupiter'):

        image_dir = '/opt/images'

        if 'image' not in config_run:
            print('- No image was configured. Using latest: mg-pythia-delphes-latest')
            container_image = 'mg-pythia-delphes-latest'
        else:
            if ':' in config_run['image']:
                config_run['image'] = config_run['image'].replace(':', '-')

            if config_run['image'] not in available_images:
                raise ValueError(f'Image {config_run["image"]} is not available; choose from {available_images}')
            else:
                container_image = config_run["image"]

        container_image_path = f'{image_dir}/{container_image}.sif'
        print(f'- Running with condor using image: {container_image_path}')

    elif run_mode == 'local-docker':
        if 'image' in config_run:
            container_image_path = config_run['image']
        else:
            container_image_path = 'franaln/mg-pythia-delphes:latest'

        print(f'- Running locally with docker using image: {container_image_path}')

    elif run_mode == 'local-apptainer':

        if 'image' in config_run:
            if config_run['image'] in available_images and os.path.exists(f'/opt/images/{config_run["image"]}.sif'):
                container_image_path = f'/opt/images/{config_run["image"]}.sif'
            else:
                container_image_path = config_run['image']
        else:
            raise ValueError('No default Apptainer image is available')

        print(f'- Running locally with apptainer using image: {container_image_path}')

    return container_image_path

