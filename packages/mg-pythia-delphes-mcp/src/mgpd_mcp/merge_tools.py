"""Workspace-confined merge planning and bounded provenance inspection."""
import json
from pathlib import Path
import mgpd
from . import operations


def checked_output(workspace, production, output=None):
    raw = Path(output) if output else production / 'merged'
    raw = raw if raw.is_absolute() else workspace.root / raw
    if raw.is_symlink() or raw.exists():
        raise FileExistsError('Merge output already exists')
    output = workspace.path(raw)
    if not output.parent.is_dir():
        raise ValueError('Merge output parent must exist')
    if output.name.startswith('.mgpd'):
        raise ValueError('Reserved adapter output name')
    # A direct child is the conventional merged output; deeper production paths
    # risk writing inside inputs, job directories, or operation metadata.
    for ancestor in output.parents:
        if (ancestor / 'manifest.json').exists() and output.parent != ancestor:
            raise ValueError('Merge output cannot be nested in a production job/input directory')
        if ancestor == workspace.root:
            break
    return output


def plan(workspace, path, options, limit):
    production = workspace.production(path)
    output = checked_output(workspace, production, options.get('output'))
    review = mgpd.plan_production_merge(production, **{**options, 'output': str(output)})
    for key in ('selection', 'family', 'omitted_job_ids', 'samples'):
        review[key + '_total'] = len(review[key])
        review[key + '_truncated'] = len(review[key]) > limit
        review[key] = review[key][:limit]
    return review


def report(workspace, path, operation_id, limit):
    production = workspace.production(path)
    record = operations.read(workspace, production, operation_id)
    if record['operation_id'] != operation_id or record['kind'] != 'merge':
        raise ValueError('Not a merge operation for this production')
    if record['state'] != 'completed':
        raise ValueError('Merge has no completed report; inspect operation_status')
    report_path = workspace.path(Path(record['options']['output']) / 'merge-report.json')
    data = json.loads(report_path.read_text())
    if data['selection_sha256'] != record['selection_sha256']:
        raise ValueError('Merge report identity changed')
    data['report_path'] = str(report_path.relative_to(workspace.root))
    for key in ('family', 'outputs', 'omitted_jobs'):
        data[key + '_total'] = len(data[key])
        data[key + '_truncated'] = len(data[key]) > limit
        data[key] = data[key][:limit]
    data['samples_total'] = len(data['samples'])
    data['samples_truncated'] = len(data['samples']) > limit
    data['samples'] = dict(list(data['samples'].items())[:limit])
    for sample in data['samples'].values():
        sample['jobs_total'] = len(sample['jobs'])
        sample['jobs_truncated'] = len(sample['jobs']) > limit
        sample['jobs'] = sample['jobs'][:limit]
    return data
