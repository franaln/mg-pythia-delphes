"""Optional dependencies are checked only at the analysis boundary."""
from importlib.util import find_spec

RUNTIME_MODULES = ('numpy', 'awkward', 'vector', 'uproot', 'matplotlib')


def require_dependencies(modules=RUNTIME_MODULES):
    missing = [module for module in modules if find_spec(module) is None]
    if missing:
        raise ValueError('Missing analysis dependencies: ' + ', '.join(missing)
                         + ". Install with: pip install 'mg-pythia-delphes[analysis]'")
