"""Existence tests answered by a relationship-keyed count carrier.

RewriterV4 cannot consume a relationship carrier whose FILL source has a
QueryPart boundary (it does not rewrite even the FILL's own source text), so
this is a separate, local semantic rule in the style of scalar_subqueries.py.
It never matches query numbers, file names or full-text templates.

Shapes
------
A registered relationship FILL

    FILL R(X) FROM (MATCH (pA:S)-[:R]->(pB:T) WITH pA, pB
                    MATCH P' RETURN pA.key, pB.key, count(w'))

stores on every R edge e = (u -> v) the value  X(e) = m(u,v) * |W_P'(pA:=u, pB:=v)|,
where m(u,v) >= 1 is the number of R edges u -> v (the WITH keeps one row per
edge).  Hence  X(e) > 0  <=>  W_P'(u, v) is non-empty.  An edge the FILL does
not emit keeps the reset value 0, which is the same statement for count 0.

A QueryPart with input binding rho

    OPTIONAL MATCH P [WHERE p]
    WITH DISTINCT items(rho, w IS [NOT] NULL) [ORDER BY / WHERE on items]

is rewritten to

    OPTIONAL MATCH (a)-[k:R]-(b)
    WITH <input columns>, E AS e
    WITH DISTINCT items(rho, NOT e / e) ...

    E = CASE WHEN k IS NULL THEN EXISTS { MATCH P [WHERE p] }
             WHEN startNode(k) = a THEN coalesce(k.X_ab, 0) > 0
             ELSE coalesce(k.X_ba, 0) > 0 END

Correctness (theory.md, Definitions 5-7, for the existence functional)
---------------------------------------------------------------------
(M_P)(i)  The bound graph of P, with its two input anchors (a, b) mapped to
          (pA, pB) (resp. (pB, pA) for X_ba), is isomorphic to P': node and
          relationship table sets are equal, endpoints and directions are
          preserved, and fresh variables correspond one to one.  The R edge
          the index is keyed on is the edge k now matched inside this part.
(M_P)(ii) The interface is {a, b, k}: a and b are the endpoints of k, i.e.
          the index key.  Every other variable of P is fresh (hidden) and the
          rest of the query refers to them only through `w IS [NOT] NULL`.
(M_F)     Neither P nor P' has a filter after removing the relationship
          inequalities that encode Neo4j's relationship isomorphism; each
          removed inequality is implied, because the two relationships being
          equal would make an acyclic relationship of P a self-loop.  Thus
          W_P (Neo4j) = W_P' (homomorphic) as multisets.
(L)       For existence, Phi = [v > 0], by the identity X(e) > 0 above.
DISTINCT  OPTIONAL MATCH P turns rho into rows that all carry the same
          truth value of `w IS NULL` (no witness: one padded row; otherwise
          every witness has w non-null).  items use fresh variables only
          through that test and contain no aggregate, so the SET of item
          tuples from rho is the same for both forms, and WITH DISTINCT maps
          both multisets to that set.  OPTIONAL MATCH (a)-[k:R]-(b) also
          yields only rows with the same E: any R edge between a and b gives
          the same truth value, and no edge gives one row whose fallback is
          the original test verbatim (a NULL anchor makes both sides false).
The theorem does not model OPTIONAL MATCH or DISTINCT (theory.md 5.4); the
DISTINCT argument above is the separate semantic reason, recorded in
Result.proofs.  No declared constraint is used beyond acyclic_rel_types.
"""
import re
from itertools import permutations

from . import queryparts
from .engine import EngineError
from .errors import Unsupported
from .neo4j import tokenize, render, _conjuncts, _ref
from .syntax import closing, name, reference_positions, parameter_names

AGGREGATES = {'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COLLECT', 'STDEV', 'STDEVP',
              'PERCENTILECONT', 'PERCENTILEDISC'}
_FILL = re.compile(r'^\s*FILL\s+(\w+)\s*\(([^)]*)\)\s*FROM\s*\((.*)\)\s*;?\s*$', re.S | re.I)
MAX_FREE_NODES = 7


# ------------------------------------------------------------------ graphs
def _graph(clause, pairs):
    """{uid: frozenset(tables)}, [rel dicts] of one bound reading clause.

    Relationship child tables whose endpoint pair cannot match the endpoint
    nodes' tables are dropped (they contribute no rows; canonical.py applies
    the same normalization), so both sides compare as the same graph."""
    nodes, rels = {}, {}
    for graph in clause.get('graphs') or []:
        for node in graph.get('nodes') or []:
            nodes[node['uid']] = frozenset(t.upper() for t in node.get('tables') or [])
        for rel in graph.get('rels') or []:
            if rel.get('direction') != 'SINGLE' or rel.get('rel_type', 0) != 0 or rel.get('recursive'):
                raise Unsupported('carrier pattern needs fixed-length directed relationships')
            rels[rel['uid']] = dict(uid=rel['uid'], raw=rel.get('raw', ''), src=rel['src'], dst=rel['dst'],
                                    tables=[t.upper() for t in rel.get('tables') or []])
    for rel in rels.values():
        src, dst = nodes.get(rel['src']), nodes.get(rel['dst'])
        keep = [t for t in rel['tables'] if t not in pairs or src is None or dst is None
                or (pairs[t][0] in src and pairs[t][1] in dst)]
        rel['tables'] = frozenset(keep or rel['tables'])
    return nodes, list(rels.values())


def _pairs(mw):
    from .canonical import rel_pairs_of
    return {k.upper(): (v[0].upper(), v[1].upper()) for k, v in rel_pairs_of(mw.schema).items()}


def _find(parent, x):
    while parent.get(x, x) != x:
        x = parent[x]
    return x


def _redundant_inequality(r1, r2, rels, acyclic):
    """r1 = r2 would force equal endpoints; if that makes an acyclic
    relationship a self-loop, the inequality holds in every graph."""
    parent = {}
    for x, y in ((r1['src'], r2['src']), (r1['dst'], r2['dst'])):
        fx, fy = _find(parent, x), _find(parent, y)
        if fx != fy:
            parent[fx] = fy
    return any(acyclic(r) and _find(parent, r['src']) == _find(parent, r['dst']) for r in rels)


def _residual_predicate(predicate, rels, acyclic):
    """Conjuncts of `predicate` that are not implied relationship inequalities."""
    by_uid = {r['uid']: r for r in rels}
    left = []
    for c in _conjuncts(predicate):
        if c.get('function') == 'NOT_EQUALS' and len(c.get('children') or []) == 2:
            x, y = (_ref(child) for child in c['children'])
            if x in by_uid and y in by_uid and _redundant_inequality(by_uid[x], by_uid[y], rels, acyclic):
                continue
        left.append(c)
    return left


def _isomorphism(query, fill, fixed):
    """Map fill node uids -> query node uids extending `fixed`, or None."""
    (q_nodes, q_rels), (f_nodes, f_rels) = query, fill
    if len(q_nodes) != len(f_nodes) or len(q_rels) != len(f_rels):
        return None
    free_f = [u for u in f_nodes if u not in fixed]
    free_q = [u for u in q_nodes if u not in fixed.values()]
    if len(free_f) != len(free_q) or len(free_f) > MAX_FREE_NODES:
        return None
    target = sorted((tuple(sorted(r['tables'])), r['src'], r['dst']) for r in q_rels)
    for image in permutations(free_q):
        mapping = dict(fixed, **dict(zip(free_f, image)))
        if any(f_nodes[u] != q_nodes[mapping[u]] for u in f_nodes):
            continue
        mapped = sorted((tuple(sorted(r['tables'])), mapping[r['src']], mapping[r['dst']]) for r in f_rels)
        if mapped == target:
            return mapping
    return None


# ------------------------------------------------------------------ carriers
def carriers(mw):
    """Relationship count carriers among the registered FILLs."""
    out = []
    groups = mw.profile.dialect.rel_group
    for index_id, fills in mw.fills.items():
        for fill in fills:
            m = _FILL.match(re.sub(r'/\*.*?\*/', ' ', fill, flags=re.S))
            if not m:
                continue
            table, columns, source = m.group(1).upper(), [c.strip() for c in m.group(2).split(',')], m.group(3)
            try:
                found = _carrier(mw, index_id, table, columns, source, groups)
            except (Unsupported, EngineError, KeyError, TypeError, IndexError):
                continue
            out.extend(found)
    return out


def _carrier(mw, index_id, table, columns, source, groups):
    ir = mw.engine.bind(source, {})['ir']
    singles = ir.get('single_queries') or []
    if len(singles) != 1 or len(singles[0].get('parts') or []) != 2:
        return []
    head, tail = singles[0]['parts']
    if len(head.get('reading') or []) != 1 or len(tail.get('reading') or []) != 1:
        return []
    first, second = head['reading'][0], tail['reading'][0]
    if first.get('optional') or second.get('optional') or first.get('predicate') or head.get('updating'):
        return []
    pairs = _pairs(mw)
    nodes, rels = _graph(first, pairs)
    if len(nodes) != 2 or len(rels) != 1 or {groups.get(t, t).upper() for t in rels[0]['tables']} != {table}:
        return []
    src, dst = rels[0]['src'], rels[0]['dst']
    projection = head.get('projection') or {}
    if any(projection.get(k) for k in ('distinct', 'order_by', 'skip', 'limit')) or \
            [e.get('entity') for e in projection.get('expressions') or []] != [src, dst]:
        return []
    graph = _graph(second, pairs)
    if src not in graph[0] or dst not in graph[0]:
        return []
    final = tail.get('projection') or {}
    if any(final.get(k) for k in ('distinct', 'order_by', 'skip', 'limit')):
        return []
    expressions = final.get('expressions') or []
    if len(expressions) != 2 + len(columns) or \
            [e.get('owner') for e in expressions[:2]] != [src, dst] or \
            any(e.get('k') != 'PROPERTY' for e in expressions[:2]):
        return []
    out = []
    for column, e in zip(columns, expressions[2:]):
        # count(w) of a pattern node (never NULL in a mandatory match) or count(*)
        if e.get('k') != 'AGGREGATE_FUNCTION' or e.get('distinct') or \
                e.get('function') not in ('COUNT', 'COUNT_STAR'):
            continue
        children = e.get('children') or []
        if e['function'] == 'COUNT' and (len(children) != 1 or children[0].get('entity') not in graph[0]):
            continue
        out.append(dict(index_id=index_id, table=table, column=column, src=src, dst=dst,
                        src_tables=graph[0][src], dst_tables=graph[0][dst],
                        graph=graph, predicate=second.get('predicate')))
    return out


# ------------------------------------------------------------------ query side
def _clauses(tokens):
    return queryparts._top_level(tokens)


def _optional_part(tokens):
    """(pattern, where, with_start) of `OPTIONAL MATCH P [WHERE p] WITH DISTINCT ...`."""
    clauses = _clauses(tokens)
    words = [w for _, w in clauses]
    if not words or words[0] != 'OPTIONAL' or tokens[clauses[0][0] + 1].upper != 'MATCH':
        return None
    if words[1:2] == ['WHERE'] and words[2:3] == ['WITH']:
        pattern = tokens[clauses[0][0] + 2:clauses[1][0]]
        where = tokens[clauses[1][0] + 1:clauses[2][0]]
        with_at = 2
    elif words[1:2] == ['WITH']:
        pattern, where, with_at = tokens[clauses[0][0] + 2:clauses[1][0]], [], 1
    else:
        return None
    start = clauses[with_at][0]
    if start + 1 >= len(tokens) or tokens[start + 1].upper != 'DISTINCT':
        return None
    if any(w in ('SKIP', 'LIMIT', 'MATCH', 'OPTIONAL', 'UNWIND', 'CALL', 'RETURN') for w in words[with_at + 1:]):
        return None
    if any(t.text == '*' for t in pattern) or any(t.upper in ('EXISTS', 'COUNT') for t in where):
        return None
    return pattern, where, start


def _fresh_tests(tokens, start, fresh):
    """Positions (i, j, negated) of `w IS [NOT] NULL` for fresh w after `start`;
    None when a fresh variable is used any other way or an aggregate occurs."""
    tests = []
    for i, t in enumerate(tokens[start:], start):
        if t.kind == 'ident' and t.upper in AGGREGATES and i + 1 < len(tokens) and tokens[i + 1].text == '(' \
                and not (i and tokens[i - 1].text == '.'):
            return None
    for i in reference_positions(tokens):
        if i < start or name(tokens[i]) not in fresh:
            continue
        rest = [x.upper for x in tokens[i + 1:i + 4]]
        if rest[:2] == ['IS', 'NULL']:
            tests.append((i, i + 3, True))
        elif rest[:3] == ['IS', 'NOT', 'NULL']:
            tests.append((i, i + 4, False))
        else:
            return None
    return tests


def _taken(tokens):
    return {name(t) for t in tokens if t.kind in ('ident', 'quoted')}


def _fresh_name(base, taken):
    while base in taken:
        base += '_'
    taken.add(base)
    return base


def rewrite_part(mw, parts, i, types, inline, carriers_):
    """Rewritten text of parts[i], (indexes, proof), or raise Unsupported."""
    tokens = tokenize(parts[i])
    shape = _optional_part(tokens)
    if shape is None:
        raise Unsupported('not an OPTIONAL MATCH existence part')
    pattern, where, with_start = shape
    names = queryparts.projection_outputs(parts[i - 1]) if i else None
    if not names:
        raise Unsupported('existence part needs an input binding')
    prefix = ' '.join(parts[:i]) + ' RETURN ' + ', '.join(names)
    columns = mw._bind_prefix(prefix, types, inline)['columns']
    reserved = set(types) | set(inline or {}) | parameter_names(tokens)
    stub, stub_types = queryparts.stub(columns, mw.profile.dialect, names, reserved)
    clause = 'OPTIONAL MATCH ' + render(pattern) + (' WHERE ' + render(where) if where else '')
    probe = stub + ' ' + clause + ' RETURN count(*) AS cico_rows'
    internal = mw._to_internal(probe, inline)
    used = {k: v for k, v in {**types, **stub_types}.items() if k in parameter_names(tokenize(internal))}
    ir = mw.engine.bind(internal, used)['ir']
    tail = ir['single_queries'][0]['parts'][-1]
    if len(tail.get('reading') or []) != 1 or not tail['reading'][0].get('optional'):
        raise Unsupported('existence probe changed shape')
    reading = tail['reading'][0]
    q_nodes, q_rels = _graph(reading, _pairs(mw))
    raw = {}
    for graph in reading.get('graphs') or []:
        for x in (graph.get('nodes') or []) + (graph.get('rels') or []):
            raw[x['uid']] = x.get('raw', '')
    inputs = {n for n in names}
    anchors = [u for u in q_nodes if raw.get(u) in inputs]
    if len(anchors) != 2 or any(raw.get(r['uid']) in inputs for r in q_rels):
        raise Unsupported('existence pattern needs exactly two node anchors and no imported relationship')
    fresh = {raw[u] for u in list(q_nodes) + [r['uid'] for r in q_rels] if raw.get(u) and u not in anchors}
    if not any(raw.get(u) for u in q_nodes if u not in anchors):
        raise Unsupported('existence pattern lacks a named fresh node')
    tests = _fresh_tests(tokens, with_start, fresh)
    if not tests:
        raise Unsupported('fresh variables are used beyond IS NULL tests, or an aggregate sees multiplicities')
    acyclic_groups = {t.upper() for n in getattr(mw.frontend, 'acyclic', ()) for t in mw.frontend.rels.get(n, [n])}
    groups = mw.profile.dialect.rel_group
    acyclic = lambda r: bool(r['tables']) and all(groups.get(t, t).upper() in acyclic_groups for t in r['tables'])
    if _residual_predicate(reading.get('predicate'), q_rels, acyclic):
        raise Unsupported('existence pattern has a filter no carrier stores')
    a, b = sorted(anchors, key=lambda u: raw[u])
    found = {}
    for carrier in carriers_:
        f_nodes, f_rels = carrier['graph']
        if _residual_predicate(carrier['predicate'], f_rels, acyclic):
            continue
        for orient, (x, y) in (('ab', (a, b)), ('ba', (b, a))):
            if orient in found and found[orient]['table'] != carrier['table']:
                continue
            if q_nodes[x] != carrier['src_tables'] or q_nodes[y] != carrier['dst_tables']:
                continue
            if _isomorphism((q_nodes, q_rels), (f_nodes, f_rels), {carrier['src']: x, carrier['dst']: y}):
                found.setdefault(orient, carrier)
    if not found:
        raise Unsupported('no relationship carrier stores this pattern')
    tables = {c['table'] for c in found.values()}
    if len(tables) != 1:
        raise Unsupported('orientations read different relationship tables')
    d = mw.profile.dialect
    taken = _taken(tokens) | set(names) | reserved
    k = _fresh_name('cico_k', taken)
    flag = _fresh_name('cico_exists', taken)
    rel = d.rel_types(sorted(tables))
    a_name, b_name = d.ident(raw[a]), d.ident(raw[b])
    fallback = 'EXISTS { MATCH ' + render(pattern).strip() + (' WHERE ' + render(where).strip() if where else '') + ' }'
    branch = lambda orient: (f"coalesce({d.ident(k)}.{d.ident(found[orient]['column'])}, 0) > 0"
                             if orient in found else fallback)
    expression = (f'CASE WHEN {d.ident(k)} IS NULL THEN {fallback} '
                  f'WHEN startNode({d.ident(k)}) = {a_name} THEN {branch("ab")} ELSE {branch("ba")} END')
    out = list(tokens)
    for lo, hi, negated in sorted(tests, reverse=True):
        out[lo:hi] = tokenize((' (NOT ' if negated else ' (') + d.ident(flag) + ')')
    body = render(out[with_start:]).strip()
    text = (f'OPTIONAL MATCH ({a_name})-[{d.ident(k)}{rel}]-({b_name}) '
            f"WITH {', '.join(d.ident(n) for n in names)}, {expression} AS {d.ident(flag)} {body}")
    proof = dict(rule='relationship_carrier_existence', part=i + 1, anchors=[raw[a], raw[b]],
                 carriers={o: f"{c['table']}({c['column']})" for o, c in found.items()},
                 checks=['pattern_isomorphic_to_fill_source', 'no_residual_filter',
                         'relationship_inequalities_implied_by_acyclicity', 'existence_from_count',
                         'fresh_variables_only_null_tested', 'distinct_projection_without_aggregate',
                         'fallback_without_relationship'])
    return text, sorted({c['index_id'] for c in found.values()}), proof


def rewrite_parts(mw, source, types, inline):
    """{part index: (text, indexes, proof)} for every part the rule rewrites."""
    if mw.frontend.name != 'neo4j':
        return {}, []
    from .middleware import strip_comments
    parts = queryparts.split_parts(strip_comments(source))
    if not parts or len(parts) < 2:
        return {}, []
    available = None
    out, notes = {}, []
    for i in range(1, len(parts)):
        if _optional_part(tokenize(parts[i])) is None:
            continue
        if available is None:
            available = carriers(mw)
            if not available:
                return {}, []
        try:
            out[i] = rewrite_part(mw, parts, i, types, inline, available)
        except (Unsupported, EngineError) as error:
            notes.append(f'part {i + 1}: carrier existence: {str(error)[:160]}')
    return out, notes
