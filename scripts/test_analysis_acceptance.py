#!/usr/bin/env python3
"""Opt-in native ROOT acceptance and resource measurements; never simulates jobs."""
import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import awkward as ak
import numpy as np
import uproot
import yaml


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def close(actual, expected):
    np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-9)


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.report = {'state': 'running', 'input': str(args.input), 'runs': []}

    def save(self):
        (self.args.output / 'report.json').write_text(json.dumps(self.report, indent=2) + '\n')

    def run_cli(self, name, config, chunk, limit=None):
        output = self.args.output / name
        command = [str(self.args.python), '-m', 'mgpd', 'analyze', str(self.args.input),
                   '--config', str(config), '--chunk-size', str(chunk), '-o', str(output)]
        if limit is not None:
            command += ['--max-events', str(limit)]
        timing = self.args.output / (name + '-time.txt')
        env = dict(os.environ, MPLCONFIGDIR=str(self.args.output / 'mpl-cache'))
        env.pop('PYTHONPATH', None)
        started = time.perf_counter()
        with (self.args.output / (name + '.log')).open('w') as log:
            result = subprocess.run(['/usr/bin/time', '-f', '%e %M', '-o', str(timing), *command],
                                    cwd=self.args.output, env=env, stdout=log, stderr=log)
        record = {'name': name, 'command': command, 'exit_code': result.returncode,
                  'wall_seconds': time.perf_counter() - started, 'timing': str(timing)}
        self.report['runs'].append(record)
        self.save()
        if result.returncode:
            raise RuntimeError(f'{name} failed; see {name}.log')
        elapsed, rss = timing.read_text().split()
        cutflow = read_json(output / 'cutflow.json')
        record.update(elapsed_seconds=float(elapsed), peak_rss_KiB=int(rss),
                      events=cutflow[0]['n'], events_per_second=cutflow[0]['n'] / float(elapsed))
        self.save()
        return output

    def check_dileptons(self, output, limit=None):
        histograms = read_json(output / 'histograms.json')
        expected = {name: {key: np.zeros(len(h['edges'])-1) for key in ('counts', 'sumw', 'sumw2')}
                    for name, h in histograms.items()}
        weights = [[] for _ in range(4)]
        observed = {name: [] for name in histograms}
        fields = ['Electron.PT', 'Electron.Eta', 'Electron.Phi', 'Electron.Charge', 'Event.Weight']
        with uproot.open(self.args.input) as root:
            tree = root['Delphes']
            stop = min(limit, tree.num_entries) if limit else tree.num_entries
            for start in range(0, stop, 4096):
                arrays = tree.arrays(fields, entry_start=start, entry_stop=min(start+4096, stop),
                                     library='ak', how=dict)
                columns = {key: ak.to_list(value) for key, value in arrays.items()}
                values = {name: [] for name in histograms}
                fill_weights = {name: [] for name in histograms}
                for i, nominal in enumerate(columns['Event.Weight']):
                    w = nominal[0] if isinstance(nominal, list) else nominal
                    weights[0].append(w)
                    electrons = [(pt, eta, phi, q, index) for index, (pt, eta, phi, q) in enumerate(zip(
                        columns['Electron.PT'][i], columns['Electron.Eta'][i],
                        columns['Electron.Phi'][i], columns['Electron.Charge'][i])) if pt > 10 and abs(eta) < 2.5]
                    if len(electrons) < 2:
                        continue
                    weights[1].append(w)
                    pairs = [(a, b) for a, b in itertools.combinations(electrons, 2) if a[3]*b[3] < 0]
                    if not pairs:
                        continue
                    a, b = min(pairs, key=lambda pair: (-(pair[0][0]+pair[1][0]), pair[0][4], pair[1][4]))
                    vectors = [(pt*math.cos(phi), pt*math.sin(phi), pt*math.sinh(eta),
                                math.sqrt((pt*math.cosh(eta))**2 + 0.00051099895**2))
                               for pt, eta, phi, _, _ in (a, b)]
                    px, py, pz, energy = [sum(v[j] for v in vectors) for j in range(4)]
                    mass = math.sqrt(max(0., energy**2-px**2-py**2-pz**2))
                    weights[2].append(w)
                    values['m_ee_before_window'].append(mass)
                    fill_weights['m_ee_before_window'].append(w)
                    if 60 < mass < 120:
                        weights[3].append(w)
                        values['pt_ee_selected'].append(math.hypot(px, py))
                        fill_weights['pt_ee_selected'].append(w)
                for name, h in histograms.items():
                    observed[name].extend(zip(values[name], fill_weights[name]))
                    ws = np.asarray(fill_weights[name])
                    for key, fill in [('counts', None), ('sumw', ws), ('sumw2', ws**2)]:
                        expected[name][key] += np.histogram(values[name], bins=h['edges'], weights=fill)[0]
        actual = read_json(output / 'cutflow.json')
        for row, ws in zip(actual, weights):
            assert row['n'] == len(ws), row['stage']
            close([row['sumw'], row['sumw2'], row['sumabsw']],
                  [math.fsum(ws), math.fsum(w*w for w in ws), math.fsum(abs(w) for w in ws)])
        for name, h in histograms.items():
            for key in ('counts', 'sumw', 'sumw2'):
                close(h[key], expected[name][key])
            for flow, selected in [('underflow', [w for value,w in observed[name] if value < h['edges'][0]]),
                                   ('overflow', [w for value,w in observed[name] if value > h['edges'][-1]])]:
                assert h[flow]['n'] == len(selected)
                close([h[flow]['sumw'], h[flow]['sumw2']], [math.fsum(selected), math.fsum(w*w for w in selected)])
        return {'events': len(weights[0]), 'cut_counts': [len(ws) for ws in weights],
                'histograms': 'independent counts, sumw, sumw2 and flows agree'}

    def check_collections(self, starter):
        config = {'schema_version': 1, 'input': starter['input'], 'objects': {},
                  'variables': {}, 'cuts': [], 'histograms': []}
        collections = ['Electron', 'Muon', 'Photon', 'Jet', 'Particle', 'MissingET']
        for source in collections:
            key = source.lower() + '_objects'
            config['objects'][key] = {'source': source}
            config['variables'][key+'_pt'] = key+'.pt'
            config['histograms'].append({'name': key+'_pt', 'variable': key+'_pt', 'fill': 'objects',
                                         'stage': 'initial', 'bins': [0., 10.123456789, 25.123456789, 50.123456789,
                                                                     100.123456789, 250.123456789, 1000.123456789, 10000.123456789]})
        path = self.args.output / 'collections.yml'
        path.write_text(yaml.safe_dump(config))
        limit = self.args.collections_preview
        output = self.run_cli('collections', path, min(self.args.chunks), limit)
        histograms = read_json(output / 'histograms.json')
        counts = {}
        with uproot.open(self.args.input) as root:
            tree = root['Delphes']
            fields = ['Event.Weight'] + [f'{source}.{"MET" if source == "MissingET" else "PT"}' for source in collections]
            fields += ['Particle.Px', 'Particle.Py']
            arrays = tree.arrays(fields, entry_stop=limit, library='ak', how=dict)
            weights = np.array([v[0] if isinstance(v, list) else v for v in ak.to_list(arrays['Event.Weight'])])
            for source, branch in zip(collections, fields[1:7]):
                h = histograms[source.lower()+'_objects_pt']
                values = ak.to_numpy(ak.flatten(arrays[branch]))
                if source == 'Particle':
                    values = np.hypot(ak.to_numpy(ak.flatten(arrays['Particle.Px'])),
                                      ak.to_numpy(ak.flatten(arrays['Particle.Py'])))
                ws = np.repeat(weights, ak.to_numpy(ak.num(arrays[branch])))
                for key, fill in [('counts', None), ('sumw', ws), ('sumw2', ws**2)]:
                    close(h[key], np.histogram(values, bins=h['edges'], weights=fill)[0])
                assert h['underflow']['n'] == int(np.count_nonzero(values < h['edges'][0]))
                assert h['overflow']['n'] == int(np.count_nonzero(values > h['edges'][-1]))
                counts[source] = len(values)
        analysis = read_json(output / 'analysis-report.json')
        return {'events': len(weights), 'objects': counts,
                'mass_roundoff_corrections': {key: value['mass_roundoff_corrections']
                                              for key, value in analysis['objects'].items()},
                'histograms': 'native pT/MET counts/sumw/sumw2 agree'}

    def run(self):
        self.report['driver_sha256'] = digest(Path(__file__))
        (self.args.output / 'driver.py').write_bytes(Path(__file__).read_bytes())
        self.report['runtime'] = {'python': sys.version, 'uproot': uproot.__version__,
                                  'awkward': ak.__version__, 'numpy': np.__version__}
        self.report['phase'] = 'hashing_input'
        self.save()
        before = digest(self.args.input)
        self.report['input_sha256'] = before
        self.report['input_bytes'] = self.args.input.stat().st_size
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        info = subprocess.check_output([str(self.args.python), '-c',
            'import json,mgpd; from importlib.metadata import version; print(json.dumps({"module":mgpd.__file__,"version":version("mg-pythia-delphes")}))'],
            cwd=self.args.output, env=env, text=True)
        self.report['installation'] = json.loads(info)
        with uproot.open(self.args.input) as root:
            tree = root['Delphes']
            self.report['root'] = {'entries': int(tree.num_entries), 'class': tree.classname,
                                   'branches': tree.keys()}
        if self.args.preview >= self.report['root']['entries']:
            raise ValueError('--preview must be smaller than the native event count to measure a subset')
        self.report['phase'] = 'analyzing'
        self.save()
        starter = yaml.safe_load(subprocess.check_output([str(self.args.python), '-m', 'mgpd', 'analyze',
                                   '--example', 'dileptons-root'], cwd=self.args.output, env=env, text=True))
        config = self.args.output / 'dileptons.yml'
        config.write_text(yaml.safe_dump(starter))
        full = []
        for chunk in self.args.chunks:
            full.append(self.run_cli(f'root-{chunk}', config, chunk))
        for other in full[1:]:
            for name in ('cutflow.json', 'histograms.json'):
                assert read_json(full[0]/name) == read_json(other/name), f'chunk-dependent {name}'
        self.report['independent_full'] = self.check_dileptons(full[0])
        assert self.report['independent_full']['events'] == self.report['root']['entries']
        preview = self.run_cli('preview', config, min(self.args.chunks), self.args.preview)
        self.report['independent_preview'] = self.check_dileptons(preview, self.args.preview)
        self.report['collections'] = self.check_collections(starter)
        assert before == digest(self.args.input), 'source changed'
        self.report.update(state='passed', phase='complete', input_unchanged=True)
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Independent native Delphes ROOT file (read only)')
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='Python with installed wheel and analysis extra')
    parser.add_argument('--chunks', nargs='+', type=int, default=[128, 10000])
    parser.add_argument('--preview', type=int, default=10000)
    parser.add_argument('--collections-preview', type=int, default=1000)
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve(strict=True)
    # Preserve a venv's executable path rather than resolving its interpreter symlink.
    args.python = args.python.expanduser().absolute()
    args.output = args.output.expanduser().absolute()
    if (not args.input.is_file() or not args.python.is_file() or len(set(args.chunks)) < 2
            or len(set(args.chunks)) != len(args.chunks)
            or any(chunk < 1 or chunk > 1000000 for chunk in args.chunks)
            or args.preview < 1 or args.collections_preview < 1):
        parser.error('Expected an input file, Python executable, positive preview sizes and at least two distinct chunks in [1,1000000]')
    args.output.mkdir(parents=True, exist_ok=False)
    test = Acceptance(args)
    try:
        test.run()
    except Exception as exc:
        test.report.update(state='failed', error=f'{type(exc).__name__}: {exc}')
        test.save()
        raise
    print(f'Acceptance passed: {args.output}/report.json')


if __name__ == '__main__':
    main()
