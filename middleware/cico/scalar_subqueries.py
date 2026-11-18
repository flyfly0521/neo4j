"""Verified scalar graph-count regions in otherwise opaque Neo4j queries.

The QueryPart interface condition applies to EVERY correlated graph column,
not just the eventual index carrier. Compile one input binding with all of
those anchors, and accept only when the verified result has no remaining
graph reads or filters. SUM/MAX/MIN over that single input row then reduces
to its scalar argument. Outer rows, duplicates, scopes and clauses stay put.

This local count/exists lifting is a separate semantic rule, not a claim
that the wiki's additive Lean theorem covers arbitrary Neo4j expressions.
"""
from dataclasses import dataclass

from . import queryparts
from .engine import EngineError
from .errors import Unsupported
from .neo4j import tokenize, render
from .printer import Printer, contains_aggregate
from .syntax import closing, name, references, split_top, parameter_names


@dataclass
class Region:
    start: int
    end: int
    pattern: list
    predicate: list
    projection: list
    exists: bool = False


def regions(tokens, start, end):
    """Recognize syntax trees, never a workload or a full query template."""
    i = start
    while i < end:
        token = tokens[i]
        if token.kind != 'ident' or (i and tokens[i - 1].text == '.'):
            i += 1
            continue
        if token.upper == 'SIZE' and i + 2 < end and tokens[i + 1].text == '(' and tokens[i + 2].text == '[':
            hi = closing(tokens, i + 1)
            bracket = closing(tokens, i + 2)
            if hi != bracket + 1:
                i += 1
                continue
            pieces = split_top(tokens[i + 3:bracket], '|')
            if len(pieces) != 2 or not pieces[0] or pieces[0][0].text != '(':
                i = hi + 1
                continue
            body, projection = pieces
            # Dropping the projection is safe only for an identity graph
            # projection, whose evaluation cannot throw or change row count.
            if len(projection) != 1 or projection[0].kind not in ('ident', 'quoted'):
                i = hi + 1
                continue
            pattern, predicate = _where(body)
            yield Region(i, hi + 1, pattern, predicate, projection)
            i = hi + 1
            continue
        if token.upper in ('COUNT', 'EXISTS') and i + 1 < end and tokens[i + 1].text == '{':
            hi = closing(tokens, i + 1)
            body = tokens[i + 2:hi]
            if body and body[0].upper == 'MATCH':
                body = body[1:]
            pattern, predicate = _where(body)
            if pattern and pattern[0].text == '(':
                yield Region(i, hi + 1, pattern, predicate, [], token.upper == 'EXISTS')
            i = hi + 1
            continue
        i += 1


def _where(body):
    i = 0
    while i < len(body):
        if body[i].text in ('(', '[', '{'):
            i = closing(body, i) + 1
            continue
        if body[i].upper == 'WHERE':
            return body[:i], body[i + 1:]
        i += 1
    return body, []


def _variables(pattern):
    nodes, rels, anonymous = set(), set(), []
    i = 0
    while i < len(pattern):
        if pattern[i].text == '(':
            end = closing(pattern, i)
            if i + 1 < end and pattern[i + 1].kind in ('ident', 'quoted'):
                nodes.add(name(pattern[i + 1]))
            else:
                anonymous.append(i + 1)
            i = end + 1
        elif pattern[i].text == '[':
            end = closing(pattern, i)
            if i + 1 < end and pattern[i + 1].kind in ('ident', 'quoted'):
                rels.add(name(pattern[i + 1]))
            # Recursive path multiplicity has a separate proof obligation.
            if any(t.text == '*' for t in pattern[i + 1:end]):
                raise Unsupported('scalar count of recursive paths is not certified')
            i = end + 1
        else:
            if pattern[i].text not in ('-', '<-', '->', ','):
                raise Unsupported('scalar region must contain one graph pattern clause')
            i += 1
    return nodes, rels, anonymous


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _rewrite_region(mw, region, columns, types, inline):
    by_name = {n: t for n, t in columns}
    nodes, rels, anonymous = _variables(region.pattern)
    if region.projection and name(region.projection[0]) not in nodes | rels:
        raise Unsupported('pattern list must project a graph variable')
    used = references(region.pattern + region.predicate, by_name) | ((nodes | rels) & set(by_name))
    imports = [[n, t] for n, t in columns if n in used]
    anchors = [n for n, t in imports if queryparts._NODE.fullmatch(t) and not t.endswith('[]')]
    if not anchors or any(n in rels for n, _ in imports):
        raise Unsupported('scalar region needs node anchors, not imported relationships')
    if any(n not in nodes for n in anchors):
        # A node used only in a predicate must remain visible, not silently
        # disappear from the graph interface proof.
        raise Unsupported('predicate-only graph anchor')
    pattern = list(region.pattern)
    fresh = sorted(nodes - set(by_name))
    projected = name(region.projection[0]) if region.projection else None
    if projected in fresh:
        # Any fresh pattern node is non-null on every witness, so counts agree;
        # prefer the variable the source projects, which a FILL counts too.
        witness = mw.profile.dialect.ident(projected)
    elif fresh:
        witness = mw.profile.dialect.ident(fresh[0])
    elif anonymous:
        taken = {name(t) for t in pattern if t.kind in ('ident', 'quoted')}
        witness = 'cico_count_witness'
        while witness in taken or witness in by_name:
            witness += '_'
        pattern[anonymous[0]:anonymous[0]] = tokenize(witness + ' ')
    else:
        raise Unsupported('scalar count lacks a fresh non-null graph witness')
    reserved = set(types) | set(inline or {}) | parameter_names(region.pattern + region.predicate)
    stub, stub_types = queryparts.stub(imports, mw.profile.dialect, [n for n, _ in imports], reserved)
    alias = 'cico_scalar_count'
    while alias in by_name or alias in nodes | rels:
        alias += '_'
    # Group by graph identities, not an assumed `id` property: different
    # label domains can legitimately contain the same property value.
    keys = ', '.join(mw.profile.dialect.ident(n) for n in anchors)
    probe = stub + ' OPTIONAL MATCH ' + render(pattern)
    if region.predicate:
        probe += ' WHERE ' + render(region.predicate)
    probe += ' RETURN ' + keys + ', count(' + witness + ') AS ' + alias
    used_names = parameter_names(tokenize(probe))
    used_types = {k: v for k, v in {**types, **stub_types}.items() if k in used_names}
    result = mw._rewrite(probe, used_types, inline)
    if not result.rewritten:
        raise Unsupported(result.reason + ': ' + result.detail[:200])
    # Rebind AFTER the ordinary printer/round-trip guard has accepted it.
    bound = mw.engine.bind(mw._to_internal(result.cypher), used_types)['ir']
    singles = bound.get('single_queries', [])
    if len(singles) != 1 or len(singles[0].get('parts', [])) != 2:
        raise Unsupported('scalar probe changed its query-part boundary')
    head, tail = singles[0]['parts']
    if tail.get('reading') or tail.get('updating') or tail.get('predicate'):
        raise Unsupported('scalar probe still needs graph reads or filters')
    printer = Printer(mw.profile.dialect)
    printed = printer.print(bound)
    baseline = mw.engine.bind(mw._to_internal(stub + ' RETURN ' + ', '.join(
        mw.profile.dialect.ident(n) for n, _ in imports)),
                              stub_types)['ir']
    baseline_head = Printer(mw.profile.dialect).print(baseline).parts[0]
    if printed.parts[0] != baseline_head:
        raise Unsupported('scalar probe changed its input binding')
    projection = tail.get('projection') or {}
    if any(projection.get(k) for k in ('distinct', 'order_by', 'skip', 'limit')):
        raise Unsupported('scalar probe has projection modifiers')
    expressions = projection.get('expressions', [])
    if len(expressions) != len(anchors) + 1:
        raise Unsupported('scalar probe changed its output interface')
    expression = expressions[-1]
    # OPTIONAL count is total, and a fully eliminated pattern leaves one row
    # per fixed input binding. Additive/extremum aggregation of that ONE row
    # is its argument; never strip COUNT, AVG, DISTINCT or nested aggregates.
    if expression.get('k') != 'AGGREGATE_FUNCTION' or expression.get('function') not in ('SUM', 'MAX', 'MIN') \
            or expression.get('distinct') or len(expression.get('children', [])) != 1:
        raise Unsupported('scalar probe is not a singleton reducible aggregate')
    scalar = expression['children'][0]
    if contains_aggregate(scalar) or any(x.get('k') in ('SUBQUERY', 'PARAMETER', 'VARIABLE') for x in _walk(scalar)):
        raise Unsupported('scalar result has a nested aggregate or probe parameter')
    graph = {x.get('uid'): x.get('raw') for x in _walk(head)
             if x.get('kind') in ('NODE', 'REL') and 'tables' in x}
    refs = {graph.get(x.get('owner')) for x in _walk(scalar) if x.get('k') == 'PROPERTY'}
    if not refs or not refs <= set(anchors):
        raise Unsupported('scalar result reads a hidden graph binding')
    if any(x.get('entity') and graph.get(x['entity']) not in anchors for x in _walk(scalar)):
        raise Unsupported('scalar result reads a hidden graph identity')
    # Exact graph identities, not nonunique attributes or hidden objects.
    if any(graph.get(e.get('entity')) != anchor for e, anchor in zip(expressions, anchors)):
        raise Unsupported('scalar result changed its anchor keys')
    printer.alias_of = {}
    text = printer.expr(scalar)
    if parameter_names(tokenize(text)) & set(stub_types):
        raise Unsupported('scalar probe binding leaked')
    # A null outer node has no pattern witnesses. The typed probe ranges over
    # real nodes, so make this extension explicit when splicing into Neo4j.
    nulls = ' OR '.join(mw.profile.dialect.ident(n) + ' IS NULL' for n in anchors)
    text = '(CASE WHEN ' + nulls + ' THEN 0 ELSE ' + text + ' END)'
    if region.exists:
        text = '(' + text + ' > 0)'
    evidence = dict(rule='correlated_graph_count', anchors=anchors, indexes=result.indexes,
                    checks=['all_correlated_inputs_bound', 'round_trip_verified' if mw.verify else 'verification_disabled',
                            'input_binding_unchanged', 'no_remaining_graph_reads',
                            'singleton_aggregate', 'no_hidden_references', 'null_anchor_zero'])
    return text, result.indexes, evidence


def rewrite_scalar_regions(mw, cypher, types, inline):
    if mw.frontend.name != 'neo4j':
        return None, [], [], []
    tokens = tokenize(cypher)
    clauses = queryparts._top_level(tokens)
    edits, indexes, evidence, notes = [], set(), [], []
    for k, (pos, word) in enumerate(clauses):
        if word not in ('WITH', 'RETURN'):
            continue
        end = clauses[k + 1][0] if k + 1 < len(clauses) else len(tokens)
        try:
            candidates = list(regions(tokens, pos + 1, end))
        except Unsupported as error:
            notes.append(str(error))
            continue
        if not candidates:
            continue
        try:
            prefix = render(tokens[:pos]) + ' RETURN *'
            columns = mw._bind_prefix(prefix, types, inline)['columns']
        except (Unsupported, EngineError) as error:
            notes.append('scalar input: ' + str(error)[:200])
            continue
        for region in candidates:
            try:
                text, used, proof = _rewrite_region(mw, region, columns, types, inline)
                edits.append((region.start, region.end, text))
                indexes.update(used)
                evidence.append(proof)
            except (Unsupported, EngineError) as error:
                notes.append('scalar count: ' + str(error)[:200])
    for lo, hi, text in reversed(edits):
        tokens[lo:hi] = tokenize(' ' + text + ' ')
    return render(tokens) if edits else None, sorted(indexes), evidence, notes
