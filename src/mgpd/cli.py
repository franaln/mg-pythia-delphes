"""Command-line argument handling and workflow orchestration."""
import sys
import argparse
import json
from .config import load_config
from .preparation import prepare_production
from .execution import execute_production, load_prepared_production
from .condor import production_status
from .results import validate_production
from .recovery import plan_recovery, prepare_recovery
from ._version import __version__


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Run MadGraph, Pythia and Delphes locally or through HTCondor', epilog='Merge outputs: mgpd merge --help')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    # Main required arguments
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('-c', '--config', help='Configuration file')
    source.add_argument('--plan-recovery', metavar='DIRECTORY', help='Read-only recovery eligibility report')
    source.add_argument('--prepare-recovery', metavar='DIRECTORY', help='Prepare eligible jobs in a new -o directory; never submit')
    parser.add_argument('--job-id', action='append', help='Select a job for recovery; repeat as needed')
    source.add_argument('--validate-results', metavar='DIRECTORY', help='Validate saved job outputs and summarize samples')
    parser.add_argument('--event-count', choices=['exact', 'nonempty', 'ignore'], help='LHE event-count validation policy (default: exact)')
    parser.add_argument('--allow-nonpositive-cross-section', action='store_true', help='Allow finite zero/negative cross sections')
    parser.add_argument('--skip-seed-check', action='store_true', help='Do not require the planned seed in LHE headers')
    source.add_argument('--status', metavar='DIRECTORY', help='Read production status from the queue/history')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Print status as JSON')
    source.add_argument('--execute-prepared', metavar='DIRECTORY', help='Verify and execute an existing production once')
    parser.add_argument('-o', '--output', required=False, help='Output directory')
    parser.add_argument('-f', '--force', help='Force overwrite of output files', action='store_true')

    # Run options
    parser.add_argument('--run_mode', default=None, choices=['local-docker', 'local-apptainer', 'condor', 'jupiter'], help='Run mode')
    parser.add_argument('--dry-run', action='store_true', help='Prepare directory and files but don\'t run or submit jobs')

    parser.add_argument("--validate-only", action="store_true", help="Validate inputs without writing files or launching jobs")
    args = parser.parse_args(argv)
    if (args.execute_prepared or args.status or args.validate_results or args.plan_recovery) and (args.output or args.force or args.dry_run or args.validate_only or args.run_mode):
        parser.error("Saved-production actions cannot be combined with configuration/preparation options")
    if args.prepare_recovery and (args.force or args.dry_run or args.validate_only or args.run_mode):
        parser.error("Recovery preparation cannot override mode, overwrite outputs or execute work")
    if args.job_id and not args.prepare_recovery:
        parser.error("--job-id requires --prepare-recovery")
    if args.json_output and not (args.status or args.validate_results or args.plan_recovery):
        parser.error("--json requires --status, --validate-results or --plan-recovery")
    if not (args.validate_results or args.plan_recovery or args.prepare_recovery) and (args.event_count or args.allow_nonpositive_cross_section or args.skip_seed_check):
        parser.error("Output-validation policies require result validation or recovery planning/preparation")
    if not args.execute_prepared and not args.status and not args.validate_results and not args.plan_recovery and not args.validate_only and not args.output:
        parser.error("--output is required unless --validate-only is used")


    return parser, args


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == 'merge':
        from .merging import main as merge_main
        return merge_main(arguments[1:])
    parser, args = parse_args(arguments)
    if args.plan_recovery or args.prepare_recovery:
        policy = {'event_count': args.event_count or 'exact',
                  'require_positive_cross_section': not args.allow_nonpositive_cross_section,
                  'check_seed': not args.skip_seed_check}
        try:
            if args.prepare_recovery:
                child = prepare_recovery(args.prepare_recovery, args.output, job_ids=args.job_id, **policy)
                print(f'Recovery prepared: {child.output_dir}; review and use --execute-prepared to launch')
                return
            plan = plan_recovery(args.plan_recovery, **policy)
        except (OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            for job in plan['jobs']:
                print(f"{job['job_id']}: {job['state']} — {job['reason']}")
        return int(bool(plan['query_errors']))
    if args.validate_results:
        try:
            report = validate_production(args.validate_results, event_count=args.event_count or 'exact',
                                         require_positive_cross_section=not args.allow_nonpositive_cross_section,
                                         check_seed=not args.skip_seed_check)
        except (OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
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
        return int(report['state'] != 'valid')
    if args.status:
        try:
            status = production_status(args.status)
        except (OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
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
    if args.execute_prepared:
        try:
            execute_production(load_prepared_production(args.execute_prepared))
        except (ValueError, FileNotFoundError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        return
    try:
        config = load_config(args.config, args.run_mode)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print('Configuration is valid')
        return
    print(f'> Running mg-pythia-delphes with run_mode = {config["run"].get("mode", "local-docker")}. '
          f'Run name = {config["run"]["name"]}. Configuration = {args.config}')
    try:
        production = prepare_production(config, args.output, force=args.force)
    except (ValueError, FileExistsError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    if not args.dry_run:
        execute_production(production)

