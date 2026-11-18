"""Variable-length paths answered by a relation carrier (e.g. ROOT_POST).

A registered relation FILL

    FILL R() FROM (MATCH (x:X)<-[:T*l..u]-(y:Y) RETURN DISTINCT y.key, x.key)

materializes the set  R = {(y, x) : some T-walk of length l..u leads y -> x}.
A query relationship  (m)-[:T*l..u]->(p)  (anonymous, same type, direction
and bounds; endpoint tables within the FILL's) is replaced by  (m)-[:R]->(p).

Correctness (theory.md, Definitions 5-7, with the index a DISTINCT relation)
-----------------------------------------------------------------------------
For a key k = (m, p) the group G_k is the multiset of T-walks m -> p; the
relation stores only k in Read (|G_k| > 0).  The query counts every walk as
one witness, so (L) needs sum_{eta in G_k} 1 = |G_k| = Phi(v_k) = 1, i.e. at
most one walk between two nodes.  That holds when T is acyclic (walks are
paths; Neo4j trails and internal walks agree) and every T source has at most
one outgoing T (declared at_most_one): the walk from m is then determined
step by step.  (M_P): the path's interior nodes and relationships are hidden
and, being anonymous, referenced nowhere; the interface is {m, p}, the key.
(M_F): the FILL has no filter; endpoint labels of the query stay in the
pattern as residual filter h.  Neo4j's relationship uniqueness between the
path and the clause's other relationships must already be proven redundant:
the rule runs only on statements the frontend converts, and the frontend
refuses a clause whose variable-length uniqueness it cannot prove.

This substitution is a fallback: it runs only when the ordinary pipeline
(RewriterV4) leaves the statement unchanged, e.g. when NeuG's planner cannot
prepare the original pattern (README 7.7).
"""
import re

from .errors import Unsupported
from .neo4j import tokenize, render
from .syntax import closing

_FILL = re.compile(r'^\s*FILL\s+(\w+)\s*\(\s*\)\s*FROM\s*\((.*)\)\s*;?\s*$', re.S | re.I)
UNBOUNDED = 4294967295


def relation_carriers(mw):
    out = []
    groups = mw.profile.dialect.rel_group
    for index_id, fills in mw.fills.items():
        for fill in fills:
            m = _FILL.match(re.sub(r'/\*.*?\*/', ' ', fill, flags=re.S))
            if not m:
                continue
            try:
                ir = mw.engine.bind(m.group(2), {})['ir']
            except Exception:  # noqa: BLE001 - an unbindable FILL source is simply not a candidate
                continue
            singles = ir.get('single_queries') or []
            if len(singles) != 1 or len(singles[0].get('parts') or []) != 1:
                continue
            part = singles[0]['parts'][0]
            reading = part.get('reading') or []
            if len(reading) != 1 or reading[0].get('optional') or reading[0].get('predicate'):
                continue
            graphs = reading[0].get('graphs') or []
            nodes = {n['uid']: {t.upper() for t in n.get('tables') or []} for g in graphs for n in g.get('nodes') or []}
            rels = [r for g in graphs for r in g.get('rels') or []]
            if len(nodes) != 2 or len(rels) != 1:
                continue
            rel = rels[0]
            rec = rel.get('recursive') or {}
            if rel.get('direction') != 'SINGLE' or not rec or rec.get('node_predicate') or rec.get('rel_predicate') \
                    or rec.get('weighted') or rec.get('semantic', 0) != 0:
                continue
            projection = part.get('projection') or {}
            expressions = projection.get('expressions') or []
            if not projection.get('distinct') or len(expressions) != 2 or \
                    any(e.get('k') != 'PROPERTY' for e in expressions) or \
                    {e.get('owner') for e in expressions} != {rel['src'], rel['dst']}:
                continue
            type_groups = {groups.get(t, t).upper() for t in rel.get('tables') or []}
            if len(type_groups) != 1:
                continue
            aligned = expressions[0]['owner'] == rel['src']
            upper = rec.get('upper')
            out.append(dict(index_id=index_id, table=m.group(1).upper(), type=type_groups.pop(),
                            lower=rec.get('lower', 1), upper=None if upper in (None, UNBOUNDED) else upper,
                            src_tables=nodes[rel['src']], dst_tables=nodes[rel['dst']], aligned=aligned))
    return out


def _bounds(inner):
    """(lower, upper|None) of `*...` inside a relationship pattern, or None."""
    star = next((k for k, t in enumerate(inner) if t.text == '*'), None)
    if star is None:
        return None
    text = render(inner[star + 1:]).replace(' ', '')
    m = re.fullmatch(r'(\d*)(\.\.)?(\d*)', text)
    if not m:
        return None
    lo, dots, hi = m.groups()
    if not dots:
        if not lo:
            return 1, None
        return int(lo), int(lo)
    return int(lo or 1), int(hi) if hi else None


def substitute(mw, cypher):
    """(text | None, indexes, proofs, premises)."""
    f = mw.frontend
    if f.name != 'neo4j':
        return None, [], [], []
    from .middleware import strip_comments
    source = strip_comments(cypher)
    f.to_internal(source)   # raises unless every clause's uniqueness is expressible or proven redundant
    carriers = relation_carriers(mw)
    if not carriers:
        return None, [], [], []
    tokens = tokenize(source)
    f.var_tables = f._collect_var_tables(tokens)
    clauses = f.clause_table(tokens)
    facts = f.part_facts(clauses)
    edits, used, proofs, premises = [], set(), [], set()
    for _, word, _, _, _, clause_rels, _, part in clauses:
        mine = []   # (edit, proof, carrier table, src, dst) of this clause
        for rel in clause_rels:
            if not rel['varlen'] or rel['var'] is not None or not rel['directed'] or len(rel['types']) != 1:
                continue
            close = closing(tokens, rel['open'])
            span = _bounds(tokens[rel['open'] + 1:close])
            if span is None:
                continue
            name = rel['types'][0]
            internal = {t.upper() for t in f.rels.get(name, [name])}
            if name not in f.acyclic or len(internal) != 1:
                continue
            sources = {s for g in internal for s, _ in f.rel_pairs.get(g, ())}
            functional = f.constraints.at_most_one_out(name, sources)
            if functional is None:
                continue
            for c in carriers:
                if c['type'] != next(iter(internal)) or (c['lower'], c['upper']) != span:
                    continue
                src_t, dst_t = rel['src_t'], rel['dst_t']
                if not src_t or not dst_t or not {t.upper() for t in src_t} <= c['src_tables'] \
                        or not {t.upper() for t in dst_t} <= c['dst_tables']:
                    continue
                carrier = mw.profile.dialect.rel_names.get(c['table'], c['table'])
                left = tokens[rel['open'] - 1]
                left_is_src = tokens[close + 1].text == '->'    # written (src)-[...]->(dst)
                # R runs src -> dst of the path when aligned, else dst -> src.
                left_to_right = c['aligned'] == left_is_src
                replacement = f"-[:{mw.profile.dialect.ident(carrier)}]->" if left_to_right \
                    else f"<-[:{mw.profile.dialect.ident(carrier)}]-"
                proof = dict(rule='relation_carrier_path', relationship=f'{name}*{span[0]}..'
                             f'{"" if span[1] is None else span[1]}', carrier=c['table'],
                             checks=['anonymous_unreferenced_path', 'same_type_direction_bounds',
                                     'endpoint_tables_within_fill', 'acyclic_type',
                                     'at_most_one_walk_between_nodes',
                                     'clause_uniqueness_converted_or_proven'])
                # carrier edge runs path-src -> path-dst when aligned
                ends = (rel['src'], rel['dst']) if c['aligned'] else (rel['dst'], rel['src'])
                mine.append(((rel['open'] - 1, close + 2, replacement, left.ws), proof, c, functional, ends))
                break
        # Neo4j keeps relationships of one clause distinct.  Two carrier edges
        # (new, or already written in the clause) are equal only with equal
        # endpoints; that must contradict the part's filters, or the
        # substitution would add a uniqueness filter the paths did not have.
        if not mine:
            continue
        names = {mw.profile.dialect.rel_names.get(c['table'], c['table']) for _, _, c, _, _ in mine}
        existing = [(r['src'], r['dst']) for r in clause_rels
                    if not r['varlen'] and len(r.get('types') or []) == 1 and r['types'][0] in names]
        endpoints = [e for *_, e in mine] + existing
        extra = set()
        if len(endpoints) > 1:
            if word != 'MATCH':
                continue
            rels, unequal = facts.get(part, ([], set()))
            proven = True
            for i, (s1, d1) in enumerate(endpoints):
                for s2, d2 in endpoints[i + 1:]:
                    found = f.contradiction([(s1, s2), (d1, d2)], rels, unequal)
                    if found is None:
                        proven = False
                        break
                    extra |= found
                if not proven:
                    break
            if not proven:
                continue
        for edit, proof, c, functional, _ in mine:
            if len(endpoints) > 1:
                proof['checks'].append('carrier_uniqueness_implied_by_filters')
            edits.append(edit)
            proofs.append(proof)
            used.add(c['index_id'])
            premises.add(functional.describe())
        premises.update(x.describe() for x in extra)
    if not edits:
        return None, [], [], []
    for lo, hi, text, ws in sorted(edits, reverse=True):
        new = tokenize(text)
        new[0].ws = ws
        tokens[lo:hi] = new
    return render(tokens), sorted(used), proofs, sorted(premises)
