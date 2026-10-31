"""Explicit create/prepare/execute/validate/recover/merge CLI commands."""
import argparse
from contextlib import nullcontext, redirect_stdout
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import yaml

from ._version import __version__
from .projects import create_project, load_project_config, prepare_project, project_config_path
from .execution import execute_production, load_prepared_production
from .condor import production_status
from .results import validate_production
from .recovery import plan_recovery, prepare_recovery
from .merge import merge_outputs
from .production_merging import merge_production

RUN_MODES = ['local', 'local-docker', 'local-apptainer', 'condor', 'jupiter']

PROJECT_TEMPLATES = {
    'proc': 'Inline single process',
    # 'dir': 'Input directory with cards, process and run.mg5',
    # 'dirs': 'Multiple input directories',
    # 'files': 'Explicit list of files',
    # 'scan': 'Same process with different parameters defined from different param cards'
}

PROJECT_LEVELS = {
    'parton': 'Parton level (only Madgraph)',
    'particle': 'Particle level (Madgraph+Pythia)',
    'detector': 'Detector level (Madgraph+Pythia+Delphes)',
}

def _parser():
    parser = argparse.ArgumentParser(prog='mgpd', description='Create, prepare, execute and merge simulation projects')

    parser.add_argument('-v', '--version', action='version', version=f'%(prog)s {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)

    # Create project
    create = commands.add_parser('create', help='Create editable YAML/cards from a template')
    create.add_argument('directory', nargs='?', help='New project directory')
    create.add_argument('--template', choices=sorted(PROJECT_TEMPLATES))
    create.add_argument('--list-templates', action='store_true')
    create.add_argument('--name', help='Sample name; defaults to the directory name')
    create.add_argument('--level', default='parton', choices=PROJECT_LEVELS, help='')
    create.add_argument('--mode', choices=RUN_MODES, default='local')
    create.add_argument('--image')
    create.add_argument('--nevents', type=int, default=100, help='Events per job')
    create.add_argument('--njobs', type=int, default=1)
    create.add_argument('--seed', type=int, help='Fixed starting seed; default: RANDOM')
    create.add_argument('--copy', help='Copy from another config directory into another')

    # Prepare and execute production
    for name, help_text in [('prepare', 'Freeze a project into a prepared production'),
                            ('run', 'Prepare and execute/submit a project')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('project', help='Project directory or YAML file')
        command.add_argument('-o', '--output', help='Default: production/ beside the YAML')
        command.add_argument('--base-directory', help='Input path base; default: directory containing the YAML')
        command.add_argument('--mode', dest='run_mode', choices=RUN_MODES)
        command.add_argument('-f', '--force', action='store_true', help='Replace unexecuted preparation only; never reset attempts')

    execute = commands.add_parser('execute', help='Execute/submit an existing prepared production once')
    execute.add_argument('directory')

    # Check production status
    status = commands.add_parser('status', help='Read execution state from local records or Condor')
    status.add_argument('directory')
    status.add_argument('--json', action='store_true', dest='json_output')

    # Validate project configuration
    config = commands.add_parser('validate-config', help='Check a project YAML and its input files without writing')
    config.add_argument('directory', help='Project directory or YAML file')
    config.add_argument('--mode', dest='run_mode', choices=RUN_MODES)
    config.add_argument('--base-directory', help='Input path base; default: directory containing the YAML')
    config.add_argument('--json', action='store_true', dest='json_output')

    # Validate production results
    results = commands.add_parser('validate-results', help='Check completed outputs and summarize samples')
    results.add_argument('directory')
    results.add_argument('--json', action='store_true', dest='json_output')

    # Prepare production recovery
    recover = commands.add_parser('recover', help='Plan recovery or prepare selected jobs in a new production; never execute')
    recover.add_argument('directory')
    recover.add_argument('--plan', action='store_true', help='Only report eligibility; never reserve or copy jobs')
    recover.add_argument('-o', '--output', help='New child directory; required unless --plan is used')
    recover.add_argument('--job-id', action='append', help='Select an eligible job; repeat as needed (default: all eligible jobs)')
    recover.add_argument('--json', action='store_true', dest='json_output')

    # Merge production outputs
    merge = commands.add_parser('merge', help='Merge validated outputs across original/recovery attempts')
    merge.add_argument('inputs', nargs='+', help='One production directory, or explicit job output archives (.tar.gz)')
    merge.add_argument('-o', '--output', help='New output; default for a production: DIRECTORY/merged')
    merge.add_argument('--sample', action='append', help='Select a production sample; repeat as needed')
    merge.add_argument('--allow-partial', action='store_true', help='Explicitly permit omitted invalid/missing jobs')
    merge.add_argument('--event-count', choices=['exact', 'nonempty', 'ignore'], default='exact')
    merge.add_argument('--allow-nonpositive-cross-section', action='store_true')
    merge.add_argument('--skip-seed-check', action='store_true')
    merge.add_argument('-e', '--extract-lhe', action='store_true')
    merge.add_argument('-k', '--keep-all', action='store_true', help='Retain extracted inputs under all/')
    merge.add_argument('--backend', choices=['local-docker', 'local-apptainer'],
                        help='Default: Apptainer on jupiter, Docker elsewhere')
    merge.add_argument('--image', help='Override the container image/reference')

    return parser


def parse_args(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == 'create':
        if args.list_templates and args.directory:
            parser.error('--list-templates cannot be combined with a destination')
        if not args.list_templates and not args.directory:
            parser.error('create requires a new directory or --list-templates')
        if args.template is not None and args.copy is not None:
            parser.error('--copy cannot be combined with a template')

    if args.command == 'recover':
        if args.plan and (args.output or args.job_id):
            parser.error('recover --plan cannot reserve output or select jobs')
        if not args.plan and not args.output:
            parser.error('recover requires --output, or use --plan for read-only eligibility')

    return parser, args


def _print_results(report):

    print(f"Outputs: {report['state']} (recorded execution: {report['recorded_state']})")

    for job in report['jobs']:
        print(f"  {job['job_id']}: {job['state']} (execution: {job['execution_state']})")
        if job.get('error'):
            print(f"    {job['error']}")
        for kind, entry in job['outputs'].items():
            if entry.get('error'):
                print(f"    {kind}: {entry['error']}")

    for sample, summary in report['samples'].items():
        print(f"  {sample}: {summary['counts']}; validated LHE events={summary['validated_lhe_events']}, "
              f"Delphes events={summary['validated_delphes_events']}")

    for error in report['metadata_errors']:
        print(f'Metadata error: {error}', file=sys.stderr)


def main(argv=None):

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _parser().print_help()
        return 0

    parser, args = parse_args(arguments)
    try:

        if args.command == 'create':
            if args.list_templates:
                for template_name, template_description in PROJECT_TEMPLATES.items():
                    print(f"{template_name}: {template_description}")
                return 0

            if args.copy is not None:
                # project = copy_project(args.directory,
                #                      args.template,
                #                      name=args.name,
                #                      mode=args.mode,
                #                      image=args.image,
                #                      nevents=args.nevents,
                #                      njobs=args.njobs,
                #                      seed=args.seed)
                pass

            else:

                template = args.template if args.template is not None else 'proc'

                project = create_project(args.directory,
                                         template=template,
                                         name=args.name,
                                         level=args.level,
                                         mode=args.mode,
                                         image=args.image,
                                         nevents=args.nevents,
                                         njobs=args.njobs,
                                         seed=args.seed
                                     )

            print(f"Created {project['template']} project: {project['directory']}")
            print(f"Edit {project['config_path']} and cards/ before running.")

            return 0

        if args.command == 'validate-config':
            config = load_project_config(args.directory, args.run_mode, args.base_directory)
            if args.json_output:
                print(json.dumps({'valid': True, 'errors': [], 'config_path': str(project_config_path(args.directory)),
                                  'mode': config['run'].get('mode', 'local')}, indent=2))
            else:
                print('Configuration is valid')
            return 0

        if args.command in ('prepare', 'run'):
            production = prepare_project(args.project, args.output, mode=args.run_mode, force=args.force,
                                         base_directory=args.base_directory)
            print(f'Production prepared: {production.output_dir}')
            if args.command == 'run':
                execute_production(production)
            return 0

        if args.command == 'execute':
            execute_production(load_prepared_production(args.directory))
            return 0

        if args.command == 'status':
            status = production_status(args.directory)
            if args.json_output:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(f"Production: {status['state']} (recorded: {status['recorded_state']})")
                for job in status['jobs']:
                    print(f"  {job['job_id']} [{job['scheduler_id'] or '-'}]: {job['state']}"
                          + (f" — {job['HoldReason']}" if job.get('HoldReason') else ''))
                if status.get('note'):
                    print(status['note'])
                for error in status['query_errors']:
                    print(f"Query error ({error['command']}): {error['error']}", file=sys.stderr)
            return int(bool(status['query_errors']))

        if args.command == 'validate-results':
            report = validate_production(args.directory)
            if args.json_output:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                _print_results(report)
            return int(report['state'] != 'valid')

        if args.command == 'recover':
            if args.plan:
                plan = plan_recovery(args.directory)
                if args.json_output:
                    print(json.dumps(plan, indent=2, sort_keys=True))
                else:
                    for job in plan['jobs']:
                        print(f"{job['job_id']}: {job['state']} — {job['reason']}")
                return int(bool(plan['query_errors']))
            with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
                child = prepare_recovery(args.directory, args.output, job_ids=args.job_id)
            if args.json_output:
                print(json.dumps({'directory': child.output_dir, 'production_id': child.manifest['production_id'],
                                  'state': child.manifest['state'], 'jobs': child.manifest['jobs']}, indent=2))
            else:
                print(f'Recovery prepared: {child.output_dir}; review and use mgpd execute to launch')
            return 0

        if args.command == 'merge':

            try:
                if len(args.inputs) == 1 and Path(args.inputs[0]).is_dir():
                    result = merge_production(args.inputs[0], args.output, samples=args.sample,
                                              allow_partial=args.allow_partial, backend=args.backend, image=args.image,
                                              extract_lhe=args.extract_lhe, keep_all=args.keep_all,
                                              event_count=args.event_count,
                                              require_positive_cross_section=not args.allow_nonpositive_cross_section,
                                              check_seed=not args.skip_seed_check)
                else:
                    if not args.output:
                        parser.error('Explicit archive merging requires --output')
                    if args.sample or args.allow_partial or args.event_count != 'exact' or args.allow_nonpositive_cross_section or args.skip_seed_check:
                        parser.error('Sample selection, partial merging and validation policies require a production directory')

                    result = merge_outputs(args.inputs, args.output, backend=args.backend, image=args.image,
                                   extract_lhe=args.extract_lhe, keep_all=args.keep_all)
            except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
                print(f'Error: {exc}', file=sys.stderr)
                return 1

            print(f'Merged outputs: {result}')
            return 0


    except (OSError, ValueError, TypeError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        if args.command == 'validate-config' and args.json_output:
            print(json.dumps({'valid': False, 'config_path': args.directory, 'errors': [str(exc)]}, indent=2))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 1

    return 0
