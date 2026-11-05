"""Shared scalar event engine over bounded LHE or Delphes batches."""
from .evaluation import Expression, boolean, finite
from .expressions import FIELDS
from .readers import iter_lhe_batches
from .root_reader import iter_root_batches
from .statistics import Counter, Histogram, efficiency


def calculate_analysis(plan):
    config = plan['config']
    object_filters = {key: Expression(obj['select'], f'objects.{key}.select')
                      for key, obj in config['objects'].items() if 'select' in obj}
    variables = {key: Expression(config['variables'][key], f'variables.{key}') for key in plan['variable_order']}
    cuts = [(cut['name'], Expression(cut['expression'], f"cuts.{cut['name']}")) for cut in config['cuts']]
    counters = {'initial': Counter(), **{key: Counter() for key, _ in cuts}}
    histograms = {hist['name']: Histogram(hist) for hist in config['histograms']}
    invalid_cuts = {key: 0 for key, _ in cuts}
    objects_report = {key: {'input_objects': 0, 'selected_objects': 0, 'missing_selection_values': 0}
                      for key in config['objects']}
    missing_variables = {key: 0 for key in variables}
    processed = batches = 0
    files_read = []
    maximum = plan['max_events']
    unit = config['input']['weights']['mode'] == 'unit'
    for source in plan['inputs']:
        if maximum is not None and processed >= maximum:
            break
        remaining = None if maximum is None else maximum - processed
        files_read.append(source['path'])
        if plan['format'] == 'lhe':
            reader = iter_lhe_batches(source['path'], plan['chunk_size'], remaining)
        else:
            reader = iter_root_batches(source['path'], config, plan['required_fields'], plan['chunk_size'], remaining)
        for batch in reader:
            batches += 1
            for event in batch:
                try:
                    weight = 1.0 if unit else event['weight']
                    environment = {}
                    for key, obj in config['objects'].items():
                        selected = []
                        particles = event['particles'] if plan['format'] == 'lhe' else event[key]
                        objects_report[key]['input_objects'] += len(particles)
                        for particle in particles:
                            if key in object_filters:
                                fields = {field: finite(getattr(particle, field)) for field in FIELDS}
                                decision = boolean(object_filters[key](fields))
                                objects_report[key]['missing_selection_values'] += decision is None
                                if decision is not True:
                                    continue
                            selected.append(particle)
                        # Stable sort preserves original index order for ties; missing keys sort last.
                        def sort_key(particle):
                            value = finite(getattr(particle, obj['sort_by']))
                            return (value is None, -value if value is not None and obj['descending'] else value)
                        environment[key] = sorted(selected, key=sort_key)
                        objects_report[key]['selected_objects'] += len(selected)
                    for key, expression in variables.items():
                        environment[key] = expression(environment)
                        value = environment[key]
                        missing_variables[key] += value is None or isinstance(value, list) and any(item is None for item in value)
                    accepted = {'initial'}
                    counters['initial'].add(weight)
                    for key, expression in cuts:
                        decision = boolean(expression(environment))
                        if decision is None:
                            invalid_cuts[key] += 1
                        if decision is not True:
                            break
                        counters[key].add(weight)
                        accepted.add(key)
                    for histogram in histograms.values():
                        definition = histogram.definition
                        if definition['stage'] not in accepted:
                            continue
                        histogram.selected_events += 1
                        value = environment[definition['variable']]
                        if definition['fill'] == 'objects':
                            if not isinstance(value, list):
                                raise ValueError(f"histograms.{definition['name']}: fill: objects requires a numeric collection")
                            for item in value:
                                histogram.fill(item, weight)
                        else:
                            if isinstance(value, list):
                                raise ValueError(f"histograms.{definition['name']}: collection values require fill: objects")
                            histogram.fill(value, weight)
                    processed += 1
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f"{event['source']}: event {event['event_index']}: {exc}") from exc
    cutflow = []
    previous = counters['initial']
    for key, counter in counters.items():
        cutflow.append({'stage': key, **counter.report(), 'missing_cut_values': invalid_cuts.get(key, 0),
                        'relative': efficiency(counter, previous, unit),
                        'cumulative': efficiency(counter, counters['initial'], unit)})
        previous = counter
    return {'events_processed': processed, 'files_read': files_read, 'batches_processed': batches,
            'preview_requested': maximum is not None,
            'limit_reached': maximum is not None and processed >= maximum,
            'input_exhaustion_verified': maximum is None or processed < maximum,
            'objects': objects_report, 'missing_variable_events': missing_variables,
            'cutflow': cutflow, 'histograms': {key: histogram.report() for key, histogram in histograms.items()}}
