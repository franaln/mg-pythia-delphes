"""Interpreter for the validated analysis AST; no Python code evaluation."""
import ast
import itertools
import math
import operator

from .readers import Momentum


def finite(value):
    if isinstance(value, complex) or isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _map(function, *values):
    lists = [value for value in values if isinstance(value, list)]
    if lists:
        length = len(lists[0])
        if any(len(value) != length for value in lists):
            raise ValueError('incompatible collection shapes')
        return [_map(function, *(value[i] if isinstance(value, list) else value for value in values))
                for i in range(length)]
    return function(*values)


def _numeric(function, *values):
    if any(value is None for value in values):
        return None
    if any(type(value) not in (int, float) for value in values):
        raise ValueError('numeric operations require scalar numbers or numeric collections')
    try:
        return finite(function(*(float(value) for value in values)))
    except (ArithmeticError, ValueError):
        return None


def boolean(value):
    if value is not None and type(value) is not bool:
        raise ValueError('selection must be a boolean scalar, not an object/number/collection')
    return value


def _logic(left, right, operation):
    boolean(left); boolean(right)
    if operation == 'and':
        return False if left is False or right is False else None if left is None or right is None else True
    return True if left is True or right is True else None if left is None or right is None else False


def _field(value, key):
    if value is None:
        return None
    if not isinstance(value, Momentum):
        raise ValueError('fields require a particle or four-vector')
    return finite(getattr(value, key))


def _pair(particles):
    if not isinstance(particles, list) or any(not isinstance(p, Momentum) for p in particles):
        raise ValueError('leading_os_pair requires a particle collection')
    if len(particles) < 2:
        return None
    if any(p.charge is None for p in particles):
        raise ValueError('opposite-sign pairing requires known charges; restrict the PDG selection')
    pairs = ((a, b) for a, b in itertools.combinations(particles, 2) if a.charge * b.charge < 0)
    pair = min(pairs, key=lambda pair: (-(pair[0].pt + pair[1].pt),
                                      tuple(sorted((pair[0].index, pair[1].index)))), default=None)
    if pair is None:
        return None
    a, b = pair
    values = (a.px + b.px, a.py + b.py, a.pz + b.pz, a.energy + b.energy)
    return Momentum(*values) if all(math.isfinite(value) for value in values) else None


def _delta_phi(a, b):
    def value(item):
        return _field(item, 'phi') if isinstance(item, Momentum) else item
    return _numeric(lambda x, y: (x - y + math.pi) % (2 * math.pi) - math.pi, value(a), value(b))


def _delta_r(a, b):
    if a is None or b is None:
        return None
    return _numeric(math.hypot, _delta_phi(a, b), _numeric(operator.sub, _field(a, 'eta'), _field(b, 'eta')))


def _function(name, arguments):
    if name == 'leading_os_pair':
        return _pair(arguments[0])
    if name == 'count':
        if not isinstance(arguments[0], list):
            raise ValueError('count requires a collection')
        return len(arguments[0])
    if name == 'is_valid':
        return _map(lambda value: value is not None and not (isinstance(value, float) and not math.isfinite(value)), arguments[0])
    if name == 'abs':
        return _map(lambda value: _numeric(abs, value), arguments[0])
    if name in ('mass', 'pt'):
        return _map(lambda value: _field(value, name), arguments[0])
    if name == 'delta_phi':
        return _map(_delta_phi, *arguments)
    if name == 'delta_r':
        return _map(_delta_r, *arguments)
    raise ValueError(f'unsupported function: {name}')


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


class Expression:
    def __init__(self, text, location):
        self.tree = ast.parse(text, mode='eval').body
        self.location = location

    def __call__(self, environment):
        try:
            return self._visit(self.tree, environment)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f'{self.location}: {exc}') from exc

    def _visit(self, node, env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Attribute):
            return _map(lambda item: _field(item, node.attr), env[node.value.id])
        if isinstance(node, ast.Call):
            return _function(node.func.id, [self._visit(arg, env) for arg in node.args])
        if isinstance(node, ast.BinOp):
            a, b = self._visit(node.left, env), self._visit(node.right, env)
            if isinstance(node.op, (ast.BitAnd, ast.BitOr)):
                operation = 'and' if isinstance(node.op, ast.BitAnd) else 'or'
                return _map(lambda x, y: _logic(x, y, operation), a, b)
            return _map(lambda x, y: _numeric(_BINARY[type(node.op)], x, y), a, b)
        if isinstance(node, ast.UnaryOp):
            value = self._visit(node.operand, env)
            if isinstance(node.op, (ast.Not, ast.Invert)):
                return _map(lambda x: None if boolean(x) is None else not x, value)
            function = operator.neg if isinstance(node.op, ast.USub) else operator.pos
            return _map(lambda x: _numeric(function, x), value)
        if isinstance(node, ast.BoolOp):
            values = [self._visit(value, env) for value in node.values]
            result = values[0]
            for value in values[1:]:
                result = _map(lambda a, b: _logic(a, b, 'and' if isinstance(node.op, ast.And) else 'or'), result, value)
            return result
        if isinstance(node, ast.Compare):
            previous, result = self._visit(node.left, env), True
            for operation, other in zip(node.ops, node.comparators):
                current = self._visit(other, env)
                compared = _map(lambda a, b: None if a is None or b is None else _COMPARE[type(operation)](a, b), previous, current)
                result = _map(lambda a, b: _logic(a, b, 'and'), result, compared)
                previous = current
            return result
        raise ValueError('unsupported expression element')
