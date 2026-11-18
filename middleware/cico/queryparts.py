"""QueryPart scope (wiki: "QueryPart Binding 与 Index 定义",
"QueryPart 索引改写的语义等价定理与 Lean 形式化").

A query is a chain of QueryParts split at top-level `WITH` (the last part
ends with `RETURN`).  Part i reads the binding B_{i-1} = (S_{i-1}, L_{i-1})
projected by part i-1; Theorem 1 states the rewrite of one part is
equivalent for *every* input binding B.  Rewriting each part on its own and
splicing the rewritten text back therefore preserves the whole query:
B_i = C_i(M_{P_i}(B_{i-1})) = C'_i(M'_i(B_{i-1})) for every i.

To decide the rewrite of part i with nothing but S_{i-1} (the theorem's
setting), the middleware compiles a *probe*:

    <stub binding S_{i-1}>  <part i>  RETURN <outputs of part i>

The stub only binds variables of the right types and labels (node columns
by a label match, node lists by collect, scalars by typed parameters); it
carries no information about L_{i-1} (its WITH ends in `SKIP 0`, an identity
that keeps it a separate QueryPart).  Nothing outside part i is printed
back: the stub and the trailing RETURN must come back unchanged.
"""
import re

from .errors import Unsupported

CLAUSE_STARTS = {'MATCH', 'OPTIONAL', 'UNWIND', 'WITH', 'RETURN', 'CALL', 'UNION', 'CREATE', 'MERGE',
                 'SET', 'DELETE', 'DETACH', 'REMOVE', 'FOREACH', 'LOAD'}
PROJECTION_TAIL = {'ORDER', 'SKIP', 'LIMIT', 'WHERE'}


def _tok():
    from .neo4j import tokenize, render
    return tokenize, render


def _top_level(tokens):
    """[(index, upper keyword)] of top-level clause keywords."""
    out, depth = [], 0
    for i, t in enumerate(tokens):
        if t.text in ('(', '[', '{'):
            depth += 1
        elif t.text in (')', ']', '}'):
            depth -= 1
        elif depth == 0 and t.kind == 'ident' and t.upper in CLAUSE_STARTS:
            if t.upper == 'MATCH' and out and out[-1][1] == 'OPTIONAL' and out[-1][0] == i - 1:
                continue
            # `ORDER BY x.with` cannot occur; a bare WITH/RETURN after `.` is a property
            if i > 0 and tokens[i - 1].text == '.':
                continue
            out.append((i, t.upper))
    return out


def split_parts(text):
    """Source text -> [part text] (comments must already be stripped), or
    None when the statement is outside the supported shape (UNION, CALL,
    writes, `WITH *`)."""
    tokenize, render = _tok()
    tokens = tokenize(text.strip().rstrip(';'))
    if not tokens:
        return None
    clauses = _top_level(tokens)
    words = {w for _, w in clauses}
    if words & {'UNION', 'CREATE', 'MERGE', 'SET', 'DELETE', 'DETACH', 'REMOVE', 'FOREACH', 'LOAD'}:
        return None
    if not clauses or clauses[0][0] != 0 or clauses[-1][1] == 'WITH':
        return None
    parts, start = [], 0
    for k, (i, word) in enumerate(clauses):
        if word not in ('WITH', 'RETURN'):
            continue
        end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
        if i + 1 < len(tokens) and tokens[i + 1].text == '*':
            return None
        parts.append(render(tokens[start:end]))
        start = end
    if start != len(tokens):
        return None
    return parts


def has_match(part):
    tokenize, _ = _tok()
    return any(w == 'MATCH' or w == 'OPTIONAL' for _, w in _top_level(tokenize(part)))


def projection_outputs(part):
    """Output column names of the part's final WITH/RETURN, or None."""
    tokenize, _ = _tok()
    tokens = tokenize(part)
    clauses = _top_level(tokens)
    if not clauses or clauses[-1][1] not in ('WITH', 'RETURN'):
        return None
    i = clauses[-1][0] + 1
    if i < len(tokens) and tokens[i].upper == 'DISTINCT':
        i += 1
    items, current, depth = [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.text in ('(', '[', '{'):
            depth += 1
        elif t.text in (')', ']', '}'):
            depth -= 1
        if depth == 0 and t.kind == 'ident' and t.upper in PROJECTION_TAIL:
            break
        if depth == 0 and t.text == ',':
            items.append(current)
            current = []
        else:
            current.append(t)
        i += 1
    items.append(current)
    names = []
    for item in items:
        if len(item) >= 3 and item[-2].upper == 'AS' and item[-1].kind in ('ident', 'quoted'):
            names.append(item[-1].text)
        elif len(item) == 1 and item[0].kind in ('ident', 'quoted'):
            names.append(item[0].text)
        else:
            return None
    return names


_NODE = re.compile(r'NODE\(LABELS\(([^()]*)\)\)(\[\])?$')
_SCALAR = re.compile(r'(BOOL|INT64|INT32|INT16|INT8|UINT64|UINT32|DOUBLE|FLOAT|STRING|DATE|TIMESTAMP|INTERVAL)'
                     r'(\[\])?$')


def stub(columns, dialect, names, reserved=()):
    """Source-dialect text binding the input schema S_{i-1}, and the types
    of the parameters it uses.  `columns` are the engine's [[name, type]] of
    the bound prefix; `names` the source spellings of the same columns."""
    if len(columns) != len(names):
        raise Unsupported('prefix columns')
    names = [n[1:-1].replace('``', '`') if n.startswith('`') else n for n in names]
    taken = set(names) | set(reserved)

    def fresh(base):
        while base in taken:
            base += '_'
        taken.add(base)
        return base

    nodes, lists, scalars, types = [], [], [], {}
    for (bound_name, type_text), name in zip(columns, names):
        node = _NODE.match(type_text)
        if node:
            tables = [t for t in node.group(1).split(',') if t]
            if not tables:
                raise Unsupported(f'input column {name} without labels')
            if node.group(2):
                helper = fresh(f'cico_in_{len(lists)}')
                lists.append((name, f'({dialect.node_pattern(helper, tables)})', helper))
            else:
                nodes.append((name, f'({dialect.node_pattern(name, tables)})'))
            continue
        if _SCALAR.match(type_text):
            parameter = fresh(f'cico_in_{name}')
            types[parameter] = type_text
            scalars.append((name, parameter))
            continue
        raise Unsupported(f'input column {name} of type {type_text}')
    patterns = [p for _, p in nodes] + [p for _, p, _ in lists]
    items = [dialect.ident(n) for n, _ in nodes] + \
        [f'collect({dialect.ident(h)}) AS {dialect.ident(n)}' for n, _, h in lists] + \
        [f'${dialect.ident(p)} AS {dialect.ident(n)}' for n, p in scalars]
    if not items:
        raise Unsupported('empty input schema')
    head = f"MATCH {', '.join(patterns)} " if patterns else ''
    # SKIP 0 is the identity on any binding multiset; it keeps the stub a
    # separate QueryPart (with a plain WITH, RewriterV4 merges it into part i
    # and, for OPTIONAL MATCH + count, returns a malformed statement).
    return f"{head}WITH {', '.join(items)} SKIP 0", types


def type_probe_prefix(text, frontend, dialect, bind_columns):
    """Infer types across declared opaque procedures, never their row values.

    Procedure contracts live in the profile. A reachable-node output requires
    a literal relationship filter closed over the bound seed's label domain.
    The compiler supplies seed types with lexical scope; source spellings and
    benchmark identities play no role. This probe must never be printed back.
    """
    from .normalize import call_args
    tokenize, render = _tok()
    tokens = tokenize(text)
    changed, edits, i = False, [], 0
    # An identity list filter preserves element types, regardless of its
    # predicate's result. Keep the actual filter unchanged in the real query.
    while i + 3 < len(tokens):
        if tokens[i].text != '[' or tokens[i + 1].kind != 'ident' or tokens[i + 2].upper != 'IN':
            i += 1
            continue
        level, end = 1, i + 1
        while end < len(tokens) and level:
            level += tokens[end].text == '['
            level -= tokens[end].text == ']'
            end += 1
        if level:
            raise Unsupported('unclosed list filter')
        body = tokens[i + 3:end - 1]
        if not body or body[0].kind != 'ident' or len(body) < 3 or body[1].upper != 'WHERE':
            raise Unsupported('list filter type unavailable')
        bars = [j for j, t in enumerate(body) if t.text == '|']
        if bars and (len(bars) != 1 or bars[0] != len(body) - 2 or body[-1].text != tokens[i + 1].text):
            raise Unsupported('list projection changes element type')
        edits.append((i, end, [body[0]]))
        changed, i = True, end
    for lo, hi, replacement in reversed(edits):
        tokens[lo:hi] = replacement

    # A graph-count expression has a known scalar type even when the backend
    # cannot execute its list comprehension. Only a TYPE probe may replace
    # its value; the actual expression remains untouched unless separately
    # rewritten by scalar_subqueries with its interface checks.
    from .scalar_subqueries import regions
    scalar_edits = []
    clauses = _top_level(tokens)
    for k, (pos, word) in enumerate(clauses):
        if word not in ('WITH', 'RETURN'):
            continue
        end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
        for region in regions(tokens, pos + 1, end):
            value = 'CAST(false AS BOOL)' if region.exists else 'CAST(0 AS INT64)'
            scalar_edits.append((region.start, region.end, tokenize(' ' + value + ' ')))
    for lo, hi, replacement in reversed(scalar_edits):
        tokens[lo:hi] = replacement
        changed = True

    i = 0
    while i < len(tokens):
        if tokens[i].upper != 'CALL' or tokens[i].kind != 'ident':
            i += 1
            continue
        start = i
        opening = next((j for j in range(i + 1, len(tokens)) if tokens[j].text in ('(', '{')), None)
        name = ''.join(t.text.strip('`') for t in tokens[i + 1:opening]).lower() if opening else ''
        contract = frontend.procedure_types.get(name)
        if not contract or opening is None or tokens[opening].text != '(':
            raise Unsupported('opaque procedure output type unavailable')
        end, args = call_args(tokens, opening)
        if len(args) != contract.get('arity'):
            raise Unsupported('procedure argument count does not match its type contract')
        config_index = contract.get('config_argument')
        if not isinstance(config_index, int) or not 0 <= config_index < len(args):
            raise Unsupported('procedure configuration contract unavailable')
        config = args[config_index]
        if not config or config[0].text != '{' or config[-1].text != '}':
            raise Unsupported('procedure configuration must be a literal map')
        items, current, depth = [], [], 0
        for token in config[1:-1]:
            depth += token.text in ('(', '[', '{')
            depth -= token.text in (')', ']', '}')
            if token.text == ',' and depth == 0:
                items.append(current); current = []
            else:
                current.append(token)
        if current:
            items.append(current)
        options = {}
        for item in items:
            if len(item) < 3 or item[1].text != ':' or item[0].kind != 'ident' or item[0].text in options:
                raise Unsupported('procedure configuration shape')
            options[item[0].text] = item[2:]
        if set(options) - set(contract.get('type_preserving_options', [])):
            raise Unsupported('procedure option output type unavailable')
        if end >= len(tokens) or tokens[end].upper != 'YIELD':
            raise Unsupported('procedure YIELD type unavailable')
        stop, patterns = end + 1, []
        while stop < len(tokens):
            field = tokens[stop].text.strip('`')
            rule = contract.get('outputs', {}).get(field)
            if not rule or rule.get('kind') != 'reachable_node':
                raise Unsupported('procedure output lacks a supported type contract')
            seed_index = rule.get('seed_argument')
            if not isinstance(seed_index, int) or not 0 <= seed_index < len(args):
                raise Unsupported('procedure seed argument contract')
            # Bind only the already-normalized prefix, so aliases, WITH scope,
            # and previous declared procedures are handled by the real binder.
            seed_query = render(tokens[:start]) + ' RETURN ' + render(args[seed_index])
            columns = bind_columns(seed_query)
            seed_type = _NODE.fullmatch(columns[0][1]) if len(columns) == 1 else None
            if not seed_type or seed_type.group(2):
                raise Unsupported('procedure seed must be one bound node')
            labels = {t for t in seed_type.group(1).split(',') if t}
            label_keys = {label.upper() for label in labels}
            relation = options.get(rule.get('relationship_option'), [])
            if len(relation) != 1 or relation[0].kind != 'string':
                raise Unsupported('procedure relationship type unavailable')
            relation = relation[0].text[1:-1]
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', relation):
                raise Unsupported('procedure relationship filter type unavailable')
            pairs = set()
            for rel in frontend.rels.get(relation, [relation]):
                pairs.update(frontend.rel_pairs.get(rel.upper(), set()))
            if not labels or not pairs or any(src not in label_keys or dst not in label_keys for src, dst in pairs):
                raise Unsupported('procedure traversal is not closed over seed labels')
            output, stop = tokens[stop].text, stop + 1
            if stop + 1 < len(tokens) and tokens[stop].upper == 'AS':
                output, stop = tokens[stop + 1].text, stop + 2
            patterns.append('(' + dialect.node_pattern(output, sorted(labels)) + ')')
            if stop >= len(tokens) or tokens[stop].text != ',':
                break
            stop += 1
        if not patterns:
            raise Unsupported('procedure YIELD columns unavailable')
        replacement = tokenize(' OPTIONAL MATCH ' + ', '.join(patterns) + ' ')
        tokens[start:stop] = replacement
        changed, i = True, start + len(replacement)
    # List concatenation preserves element types. Bind ALL operands against
    # the actual preceding scope and require the exact same list type. Do
    # not guess types, drop duplicates, or print this replacement as a query.
    from .syntax import split_top
    while True:
        replaced = False
        clauses = _top_level(tokens)
        for k, (pos, word) in enumerate(clauses):
            if word not in ('WITH', 'RETURN'):
                continue
            end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
            projection_end = end
            level = 0
            for j in range(pos + 1, end):
                level += tokens[j].text in ('(', '[', '{')
                level -= tokens[j].text in (')', ']', '}')
                if level == 0 and tokens[j].upper in PROJECTION_TAIL:
                    projection_end = j
                    break
            offset = pos + 1 + (tokens[pos + 1].upper == 'DISTINCT')
            for item in split_top(tokens[offset:projection_end]):
                expression = item[:-2] if len(item) > 2 and item[-2].upper == 'AS' else item
                operands = split_top(expression, '+')
                if len(operands) < 2:
                    offset += len(item) + 1
                    continue
                try:
                    probe = render(tokens[:pos]) + ' RETURN ' + ', '.join(
                        render(o) + ' AS cico_type_' + str(n) for n, o in enumerate(operands))
                    columns = bind_columns(probe)
                except Exception as error:
                    # Preserve internal/compiler errors rather than claiming a
                    # type fact. Known unsupported operands simply stay opaque.
                    from .engine import EngineError
                    if not isinstance(error, (Unsupported, EngineError)):
                        raise
                    if isinstance(error, EngineError):
                        from .middleware import neug_rejects
                        if not neug_rejects(error):
                            raise
                    offset += len(item) + 1
                    continue
                types = [t for _, t in columns]
                if types and len(types) == len(operands) and types[0].endswith('[]') and len(set(types)) == 1:
                    tokens[offset:offset + len(expression)] = operands[0]
                    changed, replaced = True, True
                    break
                offset += len(item) + 1
            if replaced:
                break
        if not replaced:
            break
    if not changed:
        raise Unsupported('no declared opaque type prefix')
    return render(tokens)
