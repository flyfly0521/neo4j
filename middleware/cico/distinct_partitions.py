"""DISTINCT counts over a list of partition keys, answered by a relationship count carrier.

A registered relationship FILL

    FILL R(c) FROM (MATCH P_I RETURN k1.key, k2.key, count(DISTINCT y))

stores on the edge k1 -> k2 the number of distinct y in the group (k1, k2).
A CALL branch (or QueryPart sequence) with an imported node list L

    UNWIND L AS f  MATCH P [WHERE w1]  [WITH ... WHERE w2]
    RETURN g, count(DISTINCT x) AS n

is rewritten to

    UNWIND L AS f  WITH DISTINCT <imports>, f
    MATCH (f)-[r:R]->(g)  [WHERE EXISTS { MATCH <residual of P> WHERE w1 AND w2 }]
    RETURN g, sum(r.c) AS n

Correctness (theory.md Definitions 5-7; DISTINCT handled by a separate argument)
--------------------------------------------------------------------------------
(M_P)(i)  P_I embeds into P with k1 -> f, k2 -> g, y -> x: node and
          relationship tables, directions and variable-length bounds equal.
(M_P)(ii) The remaining relationships of P (residual R) touch the image only
          at f and g, the index key; every other image node is hidden.
(M_A)(ii) w1, w2 and the projections never refer to a hidden node, except x,
          which is only projected and counted.
(M_F)     P_I has no filter; w1 and w2 become the residual filter h.
Partition For a fixed g, let X_f be the x of group (f, g).  Declared
          constraints make the path from y to k1 in P_I functional (a root
          path of an acyclic, at-most-one-outgoing type; fixed relationships
          declared at_most_one), so each x lies in exactly one f's group and
          the X_f are disjoint.  Hence count(DISTINCT x) over all f in L
          = sum over DISTINCT f of |X_f| = sum of R(c) - the WITH DISTINCT on
          f makes this hold for any list L, duplicates included.
Presence  The residual and its filters only involve g, f, residual nodes and
          imports.  A row of the original exists iff some f has a witness
          (an R edge, which the FILL writes only for non-empty groups) and
          the residual matches; EXISTS keeps exactly that, per row.
"""
import re
from itertools import permutations

from . import queryparts
from .errors import Unsupported
from .neo4j import tokenize, render
from .syntax import closing, name, reference_positions, parameter_names

_FILL = re.compile(r'^\s*FILL\s+(\w+)\s*\(([^)]*)\)\s*FROM\s*\((.*)\)\s*;?\s*$', re.S | re.I)
UNBOUNDED = 4294967295


def _graph(clause, pairs):
    nodes, rels = {}, []
    raw = {}
    for graph in clause.get('graphs') or []:
        for n in graph.get('nodes') or []:
            nodes[n['uid']] = frozenset(t.upper() for t in n.get('tables') or [])
            raw[n['uid']] = n.get('raw', '')
        for r in graph.get('rels') or []:
            if r.get('direction') != 'SINGLE':
                raise Unsupported('partition pattern needs directed relationships')
            rec = r.get('recursive')
            if rec and (rec.get('node_predicate') or rec.get('rel_predicate') or rec.get('weighted')):
                raise Unsupported('recursive relationship with predicates')
            bounds = None if not rec else (rec.get('lower'), None if rec.get('upper') in (None, UNBOUNDED)
                                           else rec.get('upper'))
            if any(x['uid'] == r['uid'] for x in rels):
                continue
            rels.append(dict(uid=r['uid'], raw=r.get('raw', ''), src=r['src'], dst=r['dst'], bounds=bounds,
                             tables=[t.upper() for t in r.get('tables') or []]))
    for r in rels:
        s, d = nodes.get(r['src']), nodes.get(r['dst'])
        keep = [t for t in r['tables'] if t not in pairs or s is None or d is None
                or (pairs[t][0] in s and pairs[t][1] in d)]
        r['tables'] = frozenset(keep or r['tables'])
    return nodes, rels, raw


def _pairs(mw):
    from .canonical import rel_pairs_of
    return {k.upper(): (v[0].upper(), v[1].upper()) for k, v in rel_pairs_of(mw.schema).items()}


def carriers(mw):
    out, groups, pairs = [], mw.profile.dialect.rel_group, _pairs(mw)
    for index_id, fills in mw.fills.items():
        for fill in fills:
            m = _FILL.match(re.sub(r'/\*.*?\*/', ' ', fill, flags=re.S))
            if not m:
                continue
            table, columns = m.group(1).upper(), [c.strip() for c in m.group(2).split(',') if c.strip()]
            if not columns:
                # `FILL R()` writes the relationship table's own properties, in order
                edge = next((e for e in mw.schema.edges if e['type_name'].upper() == table), None)
                columns = [p['property_name'] for p in (edge or {}).get('properties') or []
                           if not p['property_name'].startswith('__')]
            try:
                ir = mw.engine.bind(m.group(3), {})['ir']
            except Exception:  # noqa: BLE001 - not a candidate
                continue
            singles = ir.get('single_queries') or []
            if len(singles) != 1 or len(singles[0].get('parts') or []) != 1:
                continue
            part = singles[0]['parts'][0]
            reading = part.get('reading') or []
            if len(reading) != 1 or reading[0].get('optional') or reading[0].get('predicate'):
                continue
            projection = part.get('projection') or {}
            if any(projection.get(k) for k in ('distinct', 'order_by', 'skip', 'limit')):
                continue
            ex = projection.get('expressions') or []
            if len(ex) != 2 + len(columns) or any(e.get('k') != 'PROPERTY' for e in ex[:2]):
                continue
            try:
                nodes, rels, raw = _graph(reading[0], pairs)
            except Unsupported:
                continue
            for column, e in zip(columns, ex[2:]):
                if e.get('k') != 'AGGREGATE_FUNCTION' or e.get('function') != 'COUNT' or not e.get('distinct'):
                    continue
                child = (e.get('children') or [{}])[0]
                if child.get('entity') not in nodes:
                    continue
                out.append(dict(index_id=index_id, table=table, column=column, k1=ex[0]['owner'],
                                k2=ex[1]['owner'], y=child['entity'], nodes=nodes, rels=rels))
    return out


def _functional_path(mw, carrier):
    """Declared constraints making y -> k1 a function inside P_I, or None."""
    f, d = mw.frontend, mw.profile.dialect
    nodes, rels = carrier['nodes'], carrier['rels']
    names = lambda r: {d.rel_names.get(d.rel_group.get(t, t).upper(), t) for t in r['tables']}
    used, frontier, reached = set(), [carrier['y']], {carrier['y']}
    while frontier:
        u = frontier.pop()
        for r in rels:
            types = names(r)
            if len(types) != 1:
                continue
            t = types.pop()
            if r['src'] == u and r['dst'] not in reached:
                if r['bounds'] is not None:
                    # a root path: acyclic, every source has <= 1 outgoing, the end has none
                    sources = {s for g in {x.upper() for x in f.rels.get(t, [t])} for s, _ in f.rel_pairs.get(g, ())}
                    c = f.constraints.at_most_one_out(t, sources)
                    ends = nodes[r['dst']]
                    root = not any(s in ends for g in {x.upper() for x in f.rels.get(t, [t])}
                                   for s, _ in f.rel_pairs.get(g, ()))
                    if t in f.acyclic and c and root:
                        used.add(c)
                        reached.add(r['dst'])
                        frontier.append(r['dst'])
                else:
                    c = f.constraints.at_most_one_out(t, nodes[u])
                    if c:
                        used.add(c)
                        reached.add(r['dst'])
                        frontier.append(r['dst'])
            elif r['dst'] == u and r['src'] not in reached and r['bounds'] is None:
                c = f.constraints.at_most_one_in(t, nodes[u])
                if c:
                    used.add(c)
                    reached.add(r['src'])
                    frontier.append(r['src'])
    return used if carrier['k1'] in reached else None


def _embedding(q_nodes, q_rels, carrier, fixed):
    f_nodes, f_rels = carrier['nodes'], carrier['rels']
    free_f = [u for u in f_nodes if u not in fixed]
    free_q = [u for u in q_nodes if u not in fixed.values()]
    if len(free_f) > 6 or len(free_q) > 8:
        return None
    for image in permutations(free_q, len(free_f)):
        m = dict(fixed, **dict(zip(free_f, image)))
        if any(f_nodes[u] != q_nodes[m[u]] for u in f_nodes):
            continue
        left, used = list(q_rels), []
        ok = True
        for r in f_rels:
            match = next((x for x in left if x['tables'] == r['tables'] and x['bounds'] == r['bounds']
                          and x['src'] == m[r['src']] and x['dst'] == m[r['dst']]), None)
            if match is None:
                ok = False
                break
            left.remove(match)
            used.append(match)
        if ok:
            return m, used, left
    return None


def rewrite_branch(mw, local, names, columns, stub, stub_types, types, inline):
    """Rewritten branch text, indexes and proof, or raise Unsupported."""
    found = carriers(mw)
    if not found:
        raise Unsupported('no DISTINCT relationship carrier')
    tokens = list(local)
    clauses = _keywords(tokens)
    words = [w for _, w in clauses]
    if not words or words[0] != 'UNWIND' or words[-1] != 'RETURN' or 'MATCH' not in words:
        raise Unsupported('not an UNWIND ... MATCH ... RETURN branch')
    u = clauses[0][0]
    if not (tokens[u + 2].upper == 'AS' and tokens[u + 1].kind in ('ident', 'quoted')):
        raise Unsupported('UNWIND of an expression')
    lst, f_name = name(tokens[u + 1]), name(tokens[u + 3])
    by_name = dict(zip(names, (t for _, t in columns)))
    if lst not in by_name or not queryparts._NODE.match(by_name[lst] or '') or not by_name[lst].endswith('[]'):
        raise Unsupported('UNWIND source is not an imported node list')
    allowed = ['UNWIND', 'MATCH', 'WHERE', 'WITH', 'WHERE', 'RETURN']
    k, seq = 0, []
    for w in words:
        while k < len(allowed) and allowed[k] != w:
            k += 1
        if k == len(allowed):
            raise Unsupported(f'clause {w} outside the recognised shape')
        seq.append(w)
        k += 1
    pos = {w: [p for p, x in clauses if x == w] for w in set(words)}
    match_at = pos['MATCH'][0]
    nxt = lambda p: next((q for q, _ in clauses if q > p), len(tokens))
    pattern = tokens[match_at + 1:nxt(match_at)]
    wheres = [(p, nxt(p)) for p in pos.get('WHERE', [])]
    with_at = pos.get('WITH', [None])[0]
    ret_at = pos['RETURN'][0]
    # RETURN g, count(DISTINCT x) AS n   (exactly)
    ret = tokens[ret_at + 1:]
    items = []
    depth, cur = 0, []
    for t in ret:
        depth += t.text in ('(', '[', '{')
        depth -= t.text in (')', ']', '}')
        if depth == 0 and t.text == ',':
            items.append(cur)
            cur = []
        else:
            cur.append(t)
    items.append(cur)
    if len(items) != 2 or len(items[0]) != 1 or items[0][0].kind not in ('ident', 'quoted'):
        raise Unsupported('RETURN must be: key, count(DISTINCT x) AS name')
    agg = [t.text for t in items[1]]
    if not (len(agg) == 7 and agg[0].upper() == 'COUNT' and agg[1] == '(' and agg[2].upper() == 'DISTINCT'
            and agg[4] == ')' and agg[5].upper() == 'AS'):
        raise Unsupported('RETURN must be: key, count(DISTINCT x) AS name')
    g_name, x_name, alias = name(items[0][0]), name(items[1][3]), name(items[1][6])
    if with_at is not None:
        end = next((p for p, _ in wheres if p > with_at), ret_at)
        wi = [render(x).strip() for x in _split(tokens[with_at + 1:end])]
        if any(not re.fullmatch(r'`?\w+`?', x) for x in wi) or tokens[with_at + 1].upper == 'DISTINCT':
            raise Unsupported("intermediate WITH must project bare variables")
    # Bind the pattern with the imported binding.
    head = render(tokens[:match_at])
    probe = (stub + ' ' if stub else '') + head + ' MATCH ' + render(pattern) + ' RETURN count(*) AS cico_rows'
    internal = mw._to_internal(probe, inline)
    used = {k: v for k, v in {**types, **stub_types}.items() if k in parameter_names(tokenize(internal))}
    ir = mw.engine.bind(internal, used)['ir']
    reading = [c for part in ir['single_queries'][0]['parts'] for c in part.get('reading') or []
               if c.get('kind') == 'MATCH' and not c.get('optional')][-1]
    if reading.get('predicate'):
        raise Unsupported('pattern carries a translated filter (relationship uniqueness?)')
    q_nodes, q_rels, raw = _graph(reading, _pairs(mw))
    uid = {v: k for k, v in raw.items() if v}
    if not {f_name, g_name, x_name} <= set(uid):
        raise Unsupported('key, group or counted variable is not a pattern node')
    for c in found:
        fixed = {c['k1']: uid[f_name], c['k2']: uid[g_name], c['y']: uid[x_name]}
        if q_nodes[uid[f_name]] != c['nodes'][c['k1']] or q_nodes[uid[g_name]] != c['nodes'][c['k2']]:
            continue
        emb = _embedding(q_nodes, q_rels, c, fixed)
        if emb is None:
            continue
        mapping, image_rels, residual = emb
        image = set(mapping.values())
        hidden = image - {uid[f_name], uid[g_name]}
        if any((r['src'] in hidden or r['dst'] in hidden) for r in residual):
            continue                                   # (M_P)(ii): interface must be public
        groups = mw.profile.dialect.rel_group
        type_of = lambda r: {groups.get(t, t).upper() for t in r['tables']}
        if any(type_of(a) & type_of(b) for a in residual for b in image_rels):
            continue                                   # Neo4j uniqueness between residual and image
        hidden_names = {raw[h] for h in hidden if raw.get(h)} - {x_name}
        refs = set()
        for lo, hi in wheres:
            segment = tokens[lo + 1:hi]
            refs |= {name(segment[i]) for i in reference_positions(segment)}
        if refs & (hidden_names | {x_name}):
            continue                                   # (M_A)(ii)
        if with_at is not None and set(wi) & hidden_names:
            continue
        partition = _functional_path(mw, c)
        if partition is None:
            continue
        return _print(mw, tokens, u, f_name, g_name, alias, names, c, residual, q_nodes, raw, wheres, partition)
    raise Unsupported('no DISTINCT carrier embeds with a public interface and a proven partition')


def _keywords(tokens):
    """[(position, keyword)] of top-level clause keywords, WHERE/ORDER/SKIP/LIMIT included."""
    out, depth = [], 0
    words = {'UNWIND', 'MATCH', 'OPTIONAL', 'WHERE', 'WITH', 'RETURN', 'ORDER', 'SKIP', 'LIMIT', 'CALL', 'UNION'}
    for i, t in enumerate(tokens):
        if t.text in ('(', '[', '{'):
            depth += 1
        elif t.text in (')', ']', '}'):
            depth -= 1
        elif depth == 0 and t.kind == 'ident' and t.upper in words and not (i and tokens[i - 1].text == '.'):
            if t.upper == 'MATCH' and out and out[-1][1] == 'OPTIONAL' and out[-1][0] == i - 1:
                continue
            out.append((i, t.upper))
    return out


def _split(tokens):
    out, cur, depth = [], [], 0
    for t in tokens:
        depth += t.text in ('(', '[', '{')
        depth -= t.text in (')', ']', '}')
        if depth == 0 and t.text == ',':
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return out


def _print(mw, tokens, u, f_name, g_name, alias, names, c, residual, q_nodes, raw, wheres, partition):
    d = mw.profile.dialect
    taken = {name(t) for t in tokens if t.kind in ('ident', 'quoted')} | set(names)
    def fresh(base):
        while base in taken:
            base += '_'
        taken.add(base)
        return base
    rel = fresh('cico_part')
    node_names = {}
    for uid_, tables in q_nodes.items():
        node_names[uid_] = raw.get(uid_) or fresh('cico_n')
    def node(uid_):
        n = node_names[uid_]
        if n in (f_name, g_name) or n in names:
            return f'({d.ident(n)})'
        return '(' + d.node_pattern(n, sorted(q_nodes[uid_])) + ')'
    parts = [f"{node(r['src'])}-[{d.rel_types(sorted(r['tables']))}]->{node(r['dst'])}" for r in residual]
    conj = [render(tokens[lo + 1:hi]).strip() for lo, hi in wheres]
    imports = ', '.join(d.ident(n) for n in names if n != f_name)
    carrier = d.rel_names.get(c['table'], c['table'])
    text = (render(tokens[u:u + 4]).strip() + f' WITH DISTINCT {imports + ", " if imports else ""}{d.ident(f_name)}'
            f" MATCH ({d.ident(f_name)})-[{d.ident(rel)}:{d.ident(carrier)}]->({d.ident(g_name)})")
    if parts:
        text += ' WHERE EXISTS { MATCH ' + ', '.join(parts) + (' WHERE ' + ' AND '.join(f'({x})' for x in conj)
                                                              if conj else '') + ' }'
    elif conj:
        text += ' WHERE ' + ' AND '.join(f'({x})' for x in conj)
    text += f" RETURN {d.ident(g_name)}, sum({d.ident(rel)}.{d.ident(c['column'])}) AS {d.ident(alias)}"
    proof = dict(rule='distinct_partition_carrier', carrier=f"{c['table']}({c['column']})",
                 premises=sorted(x.describe() for x in partition),
                 checks=['carrier_pattern_embeds', 'interface_public', 'no_hidden_references',
                         'residual_types_disjoint', 'partition_functional_by_declared_constraints',
                         'list_keys_deduplicated', 'residual_as_exists'])
    mw.frontend.premises.update(x.describe() for x in partition)
    return text, [c['index_id']], proof
