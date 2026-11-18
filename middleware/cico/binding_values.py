"""Finite binding facts, retaining correlations between record fields.

This is an abstract interpretation of a small, closed expression language.
Facts describe a superset of possible input VALUES, never input row counts.
MATCH/WHERE may remove or duplicate rows; neither invalidates those facts.
Unknown expressions lose their facts. No data-dependent uniqueness is assumed.
"""
from dataclasses import dataclass

from .errors import Unsupported
from .neo4j import tokenize, render
from . import queryparts
from .syntax import closing, name, split_top, reference_positions


MAX_CASES = 32


@dataclass(frozen=True)
class Scalar:
    text: str


def value(tokens, env):
    if len(tokens) == 1:
        token = tokens[0]
        if token.kind in ('string', 'number', 'param') or token.upper in ('NULL', 'TRUE', 'FALSE'):
            return Scalar(token.text)
        if token.kind in ('ident', 'quoted'):
            return env.get(name(token))
    if len(tokens) == 2 and tokens[0].text in ('+', '-') and tokens[1].kind == 'number':
        return Scalar(render(tokens))
    if len(tokens) == 3 and tokens[1].text == '.':
        record = env.get(name(tokens[0]))
        return record.get(name(tokens[2])) if isinstance(record, dict) else None
    if tokens and tokens[0].text in ('[', '{') and closing(tokens, 0) == len(tokens) - 1:
        if tokens[0].text == '[':
            if len(tokens) == 2:
                return []
            result = [value(item, env) for item in split_top(tokens[1:-1])]
            return result if all(item is not None for item in result) else None
        result = {}
        for item in split_top(tokens[1:-1]):
            if len(item) < 3 or item[0].kind not in ('ident', 'quoted') or item[1].text != ':':
                return None
            key = name(item[0])
            if key in result:
                return None
            result[key] = value(item[2:], env)
        return result if all(item is not None for item in result.values()) else None
    return None


def _projection(tokens):
    if tokens and tokens[0].upper == 'DISTINCT':
        tokens = tokens[1:]
    # WHERE/ORDER/SKIP/LIMIT do not add values; evaluating their conditions
    # is unnecessary for a sound superset of possible bindings.
    i = 0
    while i < len(tokens):
        if tokens[i].text in ('(', '[', '{'):
            i = closing(tokens, i) + 1
            continue
        if tokens[i].upper in queryparts.PROJECTION_TAIL:
            tokens = tokens[:i]
            break
        i += 1
    return split_top(tokens)


def finite_inputs(prefix, names):
    """Possible correlated scalar tuples for CALL imports, or no proof."""
    tokens = tokenize(prefix)
    cases = [{}]
    clauses = queryparts._top_level(tokens)
    for k, (pos, word) in enumerate(clauses):
        end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
        body = tokens[pos + 1:end]
        if word == 'UNWIND':
            if len(body) < 4 or body[-2].upper != 'AS':
                return None
            output = name(body[-1])
            expanded = []
            for env in cases:
                items = value(body[:-2], env)
                if not isinstance(items, list):
                    return None
                expanded.extend({**env, output: item} for item in items)
            cases = expanded
            if len(cases) > MAX_CASES:
                return None
        elif word == 'WITH':
            projected = []
            for env in cases:
                output = {}
                for item in _projection(body):
                    if len(item) == 1 and item[0].text == '*':
                        output.update(env)
                        continue
                    if len(item) >= 3 and item[-2].upper == 'AS':
                        expression, column = item[:-2], name(item[-1])
                    elif len(item) == 1 and item[0].kind in ('ident', 'quoted'):
                        expression, column = item, name(item[0])
                    else:
                        continue
                    fact = value(expression, env)
                    if fact is not None:
                        output[column] = fact
                projected.append(output)
            cases = projected
        elif word not in ('MATCH', 'OPTIONAL'):
            # Unknown row producers/procedures can bind names. Never carry
            # preexisting facts through an unmodeled scope transition.
            return None
    out, seen = [], set()
    for env in cases:
        if not all(isinstance(env.get(n), Scalar) for n in names):
            return None
        row = {n: env[n].text for n in names}
        signature = tuple(row[n] for n in names)
        if signature not in seen:
            seen.add(signature)
            out.append(row)
    return out or None


def substitute_imports(body, values):
    """Substitute global scalar CALL imports in expression positions only.

    Imported variables cannot be rebound in a Neo4j CALL body. WITH aliases
    remain declarations, and unaliased imported columns retain their names.
    Unsupported projection shapes are refused rather than renamed silently.
    """
    tokens = list(body)
    if any(t.upper == 'CALL' and t.kind == 'ident' for t in tokens) or any(
            t.text == '[' and i + 2 < len(tokens) and tokens[i + 2].upper == 'IN'
            for i, t in enumerate(tokens)):
        raise Unsupported('nested CALL/comprehension scope requires separate binding analysis')
    clauses = queryparts._top_level(tokens)
    declarations, bare = set(), {}
    for k, (pos, word) in enumerate(clauses):
        end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
        if word in ('WITH', 'RETURN'):
            offset = pos + 1 + (tokens[pos + 1].upper == 'DISTINCT')
            for item in _projection(tokens[pos + 1:end]):
                if len(item) >= 3 and item[-2].upper == 'AS':
                    declarations.add(offset + len(item) - 1)
                    target = name(item[-1])
                    if target in values:
                        if len(item) != 3 or name(item[0]) != target:
                            raise Unsupported('CALL import redefined by projection')
                elif len(item) == 1 and name(item[0]) in values:
                    bare[offset] = name(item[0])
                elif any(t.kind in ('ident', 'quoted') and name(t) in values for t in item):
                    raise Unsupported('specialized CALL needs explicit output aliases')
                offset += len(item) + 1
        elif word in ('MATCH', 'OPTIONAL'):
            from .scalar_subqueries import _where, _variables
            pattern = tokens[pos + 1:end]
            if word == 'OPTIONAL':
                pattern = pattern[1:]
            pattern, _ = _where(pattern)
            nodes, rels, _ = _variables(pattern)
            if (nodes | rels) & set(values):
                raise Unsupported('CALL import used as a graph declaration')
        elif word == 'UNWIND' and end > pos + 2 and tokens[end - 2].upper == 'AS':
            if name(tokens[end - 1]) in values:
                raise Unsupported('CALL import redefined by UNWIND')
    edits = []
    positions = set(reference_positions(tokens))
    for i, token in enumerate(tokens):
        if token.kind not in ('ident', 'quoted') or name(token) not in values or i in declarations:
            continue
        if i not in positions:
            continue
        text = '(' + values[name(token)] + ')'
        if i in bare:
            text += ' AS ' + token.text
        edits.append((i, text))
    for i, text in reversed(edits):
        tokens[i:i + 1] = tokenize(' ' + text + ' ')
    return render(tokens)


def equal_guard(column, expression, dialect):
    column, expression = dialect.ident(column), '(' + expression + ')'
    # Cypher equality alone is too coarse for substitution: 10 = 10.0 but
    # toString(10) != toString(10.0); equal datetime instants may have different
    # local calendar dates. Restrict origins to primitive scalars at the call
    # site and preserve both their type and lossless textual representation.
    return '((' + column + ' IS NULL AND ' + expression + ' IS NULL) OR (' + column + \
        ' IS NOT NULL AND ' + expression + ' IS NOT NULL AND valueType(' + column + ') = valueType(' + \
        expression + ') AND toString(' + column + ') = toString(' + expression + ')))'
