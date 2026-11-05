"""Validate the small analysis expression language without evaluating Python."""
import ast
import math

FUNCTIONS = {'abs', 'count', 'leading_os_pair', 'mass', 'pt', 'is_valid', 'delta_phi', 'delta_r'}
FIELDS = {'pdg_id', 'status', 'charge', 'px', 'py', 'pz', 'energy', 'mass', 'pt', 'eta', 'phi'}
_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
            ast.Call, ast.Name, ast.Load, ast.Attribute, ast.Constant,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
            ast.BitAnd, ast.BitOr, ast.Invert, ast.USub, ast.UAdd, ast.Not,
            ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def expression_names(expression, symbols, location, *, object_filter=False):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 4096:
        raise ValueError(f'{location}: expected an expression of 1..4096 characters')
    try:
        tree = ast.parse(expression, mode='eval')
    except (SyntaxError, RecursionError) as exc:
        raise ValueError(f'{location}: invalid expression syntax') from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 256:
        raise ValueError(f'{location}: expression is too complex')
    references = set()
    for node in nodes:
        if not isinstance(node, _ALLOWED):
            raise ValueError(f'{location}: unsupported expression element {type(node).__name__}')
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float, bool) or (isinstance(node.value, int) and node.value.bit_length() > 128):
                raise ValueError(f'{location}: only bounded numeric/boolean constants are supported')
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise ValueError(f'{location}: constants must be finite')
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant) or type(node.right.value) not in (int, float) or not -8 <= node.right.value <= 8:
                raise ValueError(f'{location}: powers require a constant exponent between -8 and 8')
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ValueError(f'{location}: only approved positional function calls are supported')
            arity = 2 if node.func.id in ('delta_phi', 'delta_r') else 1
            if len(node.args) != arity:
                raise ValueError(f'{location}: {node.func.id} requires {arity} argument(s)')
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id not in symbols or node.attr not in FIELDS:
                raise ValueError(f'{location}: unknown object/variable field')
        if isinstance(node, ast.Name):
            if node.id in symbols:
                references.add(node.id)
            elif node.id not in FUNCTIONS and not (object_filter and node.id in FIELDS):
                raise ValueError(f'{location}: unknown name {node.id}')
    # A function name is not a variable value (e.g. `x: abs`).
    called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    if any(isinstance(node, ast.Name) and node.id in FUNCTIONS and id(node) not in called
           and not (object_filter and node.id in FIELDS) for node in nodes):
        raise ValueError(f'{location}: function names must be called')
    return references


def variable_order(variables, objects):
    symbols = set(variables) | set(objects)
    dependencies = {name: expression_names(expr, symbols, f'variables.{name}') & set(variables)
                    for name, expr in variables.items()}
    ordered = []
    while dependencies:
        ready = sorted(name for name, parents in dependencies.items() if not parents)
        if not ready:
            raise ValueError('variables: cyclic dependency: ' + ', '.join(sorted(dependencies)))
        for name in ready:
            ordered.append(name)
            del dependencies[name]
        for parents in dependencies.values():
            parents.difference_update(ready)
    return ordered
