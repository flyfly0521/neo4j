"""The NeuG surface: what internal Cypher the engine can bind.

A frontend's output must either bind in NeuG or the translation fails
(Unsupported, the QueryPart keeps its original text).  NeuG's parser/catalog
is much smaller than Kuzu's or Neo4j's, so constructs without a NeuG form are
rejected here, after translation and parameter inlining, with a reason that
names the construct.  The checks are syntactic and conservative; the engine
bind that follows remains the final authority.
"""
from .errors import Unsupported

# Surface names of NeuG's function catalog
# (src/compiler/function/function_collection.cpp), lower case.
FUNCTIONS = {
    'abs', 'cast', 'to_date', 'to_interval', 'timestamp', 'collect', 'contains', 'count', 'count_star',
    'date', 'date_part', 'datepart', 'duration', 'end_node', 'ends_with', 'equals', 'greater_than',
    'greater_than_equals', 'id', 'interval', 'is_acyclic', 'is_trail', 'label', 'labels', 'lcase',
    'length', 'less_than', 'less_than_equals', 'list_contains', 'list_creation', 'list_extract',
    'list_has', 'lower', 'negate', 'nodes', 'not_equals', 'properties', 'relationships', 'rels',
    'reverse', 'round', 'sqrt', 'start_node', 'starts_with', 'struct_extract', 'suffix', 'tolower',
    'toupper', 'ucase', 'upper', 'sum', 'avg', 'min', 'max',
}
# Words that may precede '(' without being a function call.
KEYWORDS = {
    'MATCH', 'OPTIONAL', 'WHERE', 'AND', 'OR', 'XOR', 'NOT', 'IN', 'RETURN', 'WITH', 'AS', 'IS', 'NULL',
    'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'DISTINCT', 'UNWIND', 'ORDER', 'BY', 'SKIP', 'LIMIT', 'ASC',
    'DESC', 'ASCENDING', 'DESCENDING', 'CONTAINS', 'STARTS', 'ENDS', 'UNION', 'EXISTS', 'COUNT',
    'SHORTEST', 'TRAIL', 'ACYCLIC', 'WALK',
}
SUBQUERY = {'EXISTS', 'COUNT'}


def check(text, schema=None):
    """Raise Unsupported for the first construct of `text` NeuG cannot bind."""
    from .neo4j import tokenize
    tokens = tokenize(text)
    if not tokens:
        raise Unsupported('empty statement')
    nodes, rels, props = _schema_names(schema)
    pattern_vars = _pattern_vars(tokens)
    stack = []  # (opener, index of the opener)
    for i, t in enumerate(tokens):
        prev = tokens[i - 1] if i else None
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        up = t.upper if t.kind == 'ident' else None
        if up == 'CALL':
            raise Unsupported('CALL (procedure or subquery): no NeuG equivalent')
        if t.text in ('(', '[', '{'):
            if t.text == '{':
                _check_brace(tokens, i, stack)
            if t.text == '[' and nxt is not None and (nxt.text == '(' or (
                    nxt.kind == 'ident' and i + 2 < len(tokens) and tokens[i + 2].kind == 'ident'
                    and tokens[i + 2].upper == 'IN')):
                raise Unsupported('list or pattern comprehension: no NeuG equivalent')
            stack.append((t.text, i))
            continue
        if t.text in (')', ']', '}'):
            if stack:
                stack.pop()
            continue
        if t.text == '..' and stack and stack[-1][0] == '[':
            opener = stack[-1][1]
            if not (opener > 0 and tokens[opener - 1].text in ('-', '<-')):
                raise Unsupported('list slice: no NeuG equivalent')
        if t.text == '+' and _string_operand(tokens, i):
            raise Unsupported('string concatenation needs literal operands: no NeuG equivalent')
        if t.kind == 'ident' and nxt is not None and nxt.text == '(' and not (prev is not None and prev.text == '.'):
            if up not in KEYWORDS and t.text.lower() not in FUNCTIONS:
                raise Unsupported(f'function {t.text}(): not in the NeuG catalog')
        if t.text == ':' and stack and stack[-1][0] in ('(', '[') and nxt is not None \
                and nxt.kind in ('ident', 'quoted') and nodes is not None:
            _check_labels(tokens, i, stack[-1][0], nodes, rels)
        if t.text == '.' and prev is not None and prev.kind in ('ident', 'quoted') and nxt is not None \
                and nxt.kind in ('ident', 'quoted') and props is not None:
            owner = prev.text.strip('`')
            name = nxt.text.strip('`')
            if owner in pattern_vars and name.upper() not in props:
                raise Unsupported(f'property {owner}.{name}: no such property in the internal schema')


def _check_brace(tokens, i, stack):
    prev = tokens[i - 1] if i else None
    if prev is not None and prev.kind == 'ident' and prev.upper in SUBQUERY:
        return
    # A property map of a node/relationship pattern: inside ( or [ right after
    # the variable, a label/type name, or a variable-length bound.
    if stack and stack[-1][0] in ('(', '[') and prev is not None and \
            (prev.kind in ('ident', 'quoted', 'number') or prev.text in ('(', '[', '..', '*')):
        opener = stack[-1][1]
        before = tokens[opener - 1] if opener else None
        if stack[-1][0] == '(' and before is not None and before.kind == 'ident' and before.upper not in KEYWORDS:
            pass  # '(' of a function call: a map argument
        elif stack[-1][0] == '[' and not (before is not None and before.text in ('-', '<-')):
            pass  # '[' of a list
        else:
            return
    raise Unsupported('map literal or map projection: NeuG has no map/struct literals')


def _check_labels(tokens, i, opener, nodes, rels):
    names, j = [], i
    while j + 1 < len(tokens) and tokens[j].text in (':', '|') and tokens[j + 1].kind in ('ident', 'quoted'):
        names.append(tokens[j + 1].text.strip('`'))
        j += 2
        if j < len(tokens) and tokens[j].text == '|' and j + 1 < len(tokens) and tokens[j + 1].text == ':':
            j += 1
    known = nodes if opener == '(' else rels
    for name in names:
        if name.upper() not in known:
            kind = 'label' if opener == '(' else 'relationship type'
            raise Unsupported(f'{kind} {name}: no such table in the internal schema')


def _string_operand(tokens, i):
    def is_string(k):
        return 0 <= k < len(tokens) and tokens[k].kind == 'string'
    if is_string(i - 1) or is_string(i + 1):
        return True
    # CAST(x AS STRING) + ...
    return i >= 3 and tokens[i - 1].text == ')' and tokens[i - 2].upper == 'STRING' and tokens[i - 3].upper == 'AS'


def _pattern_vars(tokens):
    """Variables declared by node or relationship patterns."""
    found = set()
    for i, t in enumerate(tokens[:-2]):
        a, b = tokens[i + 1], tokens[i + 2]
        if t.text == '(' and a.kind in ('ident', 'quoted') and b.text in (':', ')', '{'):
            found.add(a.text.strip('`'))
        if t.text == '[' and i and tokens[i - 1].text in ('-', '<-') and a.kind in ('ident', 'quoted') \
                and b.text in (':', ']', '*', '{'):
            found.add(a.text.strip('`'))
    return found


def _schema_names(schema):
    if schema is None:
        return None, None, None
    nodes = {v['type_name'].upper() for v in schema.vertices}
    rels = {e['type_name'].upper() for e in schema.edges}
    props = {'_ID'}
    for item in list(schema.vertices) + list(schema.edges):
        for p in item.get('properties') or []:
            props.add(p['property_name'].upper())
    return nodes, rels, props
