"""Workspace-bound configuration reviews and separately enabled writes."""
from pathlib import Path
import mgpd


def dispatch(workspace, request):
    operation = request['operation']
    guard = lambda data, base: workspace.config_inputs(data, Path(base))
    if operation == 'create_project':
        output = workspace.new_output(request['output'])
        options = {key: request[key] for key in ('name', 'level', 'mode', 'image', 'nevents', 'njobs', 'seed')
                   if request.get(key) is not None}
        source = request.get('copy_from')
        if source is not None:
            if request.get('template') is not None:
                raise ValueError('copy_from cannot be combined with template')
            source = workspace.path(source)
            guard_copy = lambda data, base: workspace.preparation_inputs(data, base, output)
            project = mgpd.copy_project(source, output, input_validator=guard_copy, **options)
        else:
            project = mgpd.create_project(output, template=request.get('template') or 'proc', **options)
        config = mgpd.load_project_config(project['config_path'])
        return {**project, 'directory': str(output.relative_to(workspace.root)),
                'config_path': str(Path(project['config_path']).relative_to(workspace.root)),
                'base_directory': str(output.relative_to(workspace.root)),
                'run': config['run'], 'review_required': True, 'submitted': False}
    if operation == 'apply_config_change':
        proposal = request['proposal']
        if not isinstance(proposal, dict):
            raise ValueError('A reviewed proposal is required')
        for key in ('source', 'destination', 'base_directory'):
            # Do not replace these fields: all proposal fields are checksum-bound.
            path = workspace.path(proposal[key])
            if str(path) != proposal[key]:
                raise ValueError('Proposal paths must be absolute canonical workspace paths')
        return mgpd.apply_config_change(proposal, request['change_sha256'], input_validator=guard)
    base = workspace.path(request.get('base_directory', '.'))
    raw = Path(request['path'])
    path = raw if raw.is_absolute() else base / raw
    workspace.path(path)
    if operation == 'inspect_config':
        return mgpd.inspect_config(path, base_directory=base, input_validator=guard)
    output = request.get('output')
    if output is not None:
        raw = Path(output)
        output = raw if raw.is_absolute() else workspace.root / raw
        workspace.path(output)
    else:
        output = path
    return mgpd.plan_config_change(path, output=output, base_directory=base,
                                   changes=request.get('changes'), remove=request.get('remove'),
                                   input_validator=guard)
