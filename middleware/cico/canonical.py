"""Canonical form of Bound IR v2, used to verify printed Cypher.

Two statements are equal when they have the same clauses, patterns
(order-insensitive inside one MATCH), expressions, projections and types,
after renaming graph-entity uids to printed variable names.  Engine-generated
uids, raw spellings and aliases are ignored; output names are compared via
`projection.names`; a reference to a column projected by an earlier WITH is
canonicalised as {ref: alias} (the rewriter may spell it as a VARIABLE while
re-binding yields the defining expression).
"""
import json


def _key(item):
    return json.dumps(item, sort_keys=True)


def _sorted(items):
    return sorted(items, key=_key)


def _set(items):
    unique = {_key(i): i for i in items}
    return [unique[k] for k in sorted(unique)]


def _is_entity(e):
    return e is not None and 'entity' in e


class Canonicalizer:
    def __init__(self, names, rel_pairs=None):
        self.names = names
        self.scope = {}
        # child relationship table -> (source node table, destination node table)
        self.rel_pairs = rel_pairs or {}

    def name(self, uid):
        return None if uid is None else self.names.get(uid, f'?{uid}')

    def compatible(self, rel, node_tables):
        """Drop child tables whose endpoint pair cannot match the endpoint
        nodes' tables: such children contribute no rows, so the binding
        means the same with or without them."""
        src, dst = node_tables.get(rel.get('src')), node_tables.get(rel.get('dst'))
        if src is None or dst is None:
            return sorted(rel.get('tables') or [])
        keep = []
        for table in rel.get('tables') or []:
            pair = self.rel_pairs.get(table)
            if pair is None or (pair[0] in src and pair[1] in dst) or \
                    (rel.get('direction') == 'BOTH' and pair[0] in dst and pair[1] in src):
                keep.append(table)
        return sorted(keep)

    def entity(self, e, node_tables=None):
        out = dict(kind=e['kind'], name=self.name(e['uid']), tables=sorted(e.get('tables') or []))
        if e['kind'] == 'REL' and node_tables is not None:
            out['tables'] = self.compatible(e, node_tables)
        if e['kind'] == 'REL':
            out.update(src=self.name(e.get('src')), dst=self.name(e.get('dst')),
                       direction=e.get('direction'), rel_type=e.get('rel_type'))
            if e.get('direction') == 'BOTH':
                out['src'], out['dst'] = sorted([out['src'], out['dst']], key=str)
            r = e.get('recursive')
            if r is not None:
                out['recursive'] = dict(lower=r.get('lower'), upper=r.get('upper'), semantic=r.get('semantic'),
                                        node_predicate=self.expr(r.get('node_predicate')),
                                        rel_predicate=self.expr(r.get('rel_predicate')),
                                        weighted=r.get('weighted'), unsupported=r.get('unsupported'))
        return out

    def graphs(self, graphs):
        """All patterns of a clause as one node set and one relationship set:
        the split into connected query graphs is a binder artifact."""
        nodes, rels = [], []
        for g in graphs or []:
            node_tables = {n['uid']: set(n.get('tables') or []) for n in g.get('nodes') or []}
            nodes += [self.entity(n) for n in g.get('nodes') or []]
            rels += [self.entity(r, node_tables) for r in g.get('rels') or []]
        return dict(nodes=_set(nodes), rels=_set(rels))

    def conjuncts(self, e):
        out = self.expr(e)
        if out is None:
            return []
        def flat(x):
            if x.get('function') == 'AND' or x.get('k') == 'AND':
                return [c for child in x.get('children') or [] for c in flat(child)]
            return [x]
        return _set(flat(out))

    def expr(self, e):
        if e is None:
            return None
        if e.get('uid') in self.scope and not _is_entity(e):
            return dict(ref=self.scope[e['uid']], type=e.get('type'))
        if e.get('k') == 'PROPERTY' and e.get('property') == '_ID' and 'owner' in e:
            return dict(idof=self.name(e['owner']))
        out = dict(k=e.get('k'), type=e.get('type'))
        if _is_entity(e):
            out['entity'] = self.name(e['entity'])
        for key in ('property', 'name', 'function', 'distinct', 'subquery', 'has_hint', 'unsupported'):
            if key in e:
                out[key] = e[key]
        if 'owner' in e:
            out['owner'] = self.name(e['owner'])
        if 'value' in e:
            out['value'] = e['value']
        if 'alternatives' in e:
            out['alternatives'] = [dict(when=self.expr(a.get('when')), then=self.expr(a.get('then')))
                                   for a in e['alternatives']]
            out['else'] = self.expr(e.get('else'))
        if 'graphs' in e:
            out['graphs'] = self.graphs(e['graphs'])
            out['where'] = self.expr(e.get('where'))
        out['children'] = [self.expr(c) for c in e.get('children') or []]
        if out.get('function') in ('EQUALS', 'NOT_EQUALS'):
            # `a = b` between entities compares identities: the binder spells it
            # with entity operands, `id(a) = id(b)` with their _ID properties
            # (the frontends emit the latter, see normalize.entity_id_comparisons).
            out['children'] = [dict(idof=c['entity']) if c and 'entity' in c else c for c in out['children']]
        return out

    @staticmethod
    def aliases(p):
        if not p:
            return {}
        return {e['uid']: n for e, n in zip(p.get('expressions') or [], p.get('names') or []) if not _is_entity(e)}

    def projection(self, p):
        if p is None:
            return None
        expressions = p.get('expressions') or []
        names = list(p.get('names') or [])
        if not names and expressions and all(_is_entity(e) for e in expressions):
            names = [None] * len(expressions)  # binder-synthesised WITH
        if len(names) == len(expressions):
            # A projected graph entity is named by the entity itself (the
            # printer refuses to rename one), so its column name adds nothing.
            names = [None if _is_entity(e) else n for e, n in zip(expressions, names)]
        out = dict(distinct=p.get('distinct'),
                   expressions=[self.expr(e) for e in expressions],
                   names=names,
                   group_by=_set([self.expr(e) for e in p.get('group_by') or []]),
                   aggregates=_set([self.expr(e) for e in p.get('aggregates') or []]),
                   ascending=list(p.get('ascending') or []),
                   skip=self.expr(p.get('skip')), limit=self.expr(p.get('limit')))
        incoming = self.scope
        self.scope = {**incoming, **self.aliases(p)}
        out['order_by'] = [self.expr(e) for e in p.get('order_by') or []]
        self.scope = incoming
        return out

    def clause(self, c):
        out = dict(kind=c.get('kind'), predicate=self.conjuncts(c.get('predicate')))
        if c.get('kind') == 'MATCH':
            out.update(optional=c.get('optional'), has_hint=c.get('has_hint'), graphs=self.graphs(c.get('graphs')))
        elif c.get('kind') == 'UNWIND':
            out.update(input=self.expr(c.get('in')),
                       output=dict(name=(c.get('out') or {}).get('name'), type=(c.get('out') or {}).get('type')))
        else:
            out['clause_type'] = c.get('clause_type')
        return out

    def part(self, p):
        reading = []
        for c in (self.clause(c) for c in p.get('reading') or []):
            # Consecutive mandatory MATCH clauses are one join (internal
            # semantics has no relationship uniqueness), e.g. the one-MATCH-
            # per-relationship Neo4j output.
            prev = reading[-1] if reading else None
            if prev and c['kind'] == prev['kind'] == 'MATCH' and not c.get('optional') \
                    and not prev.get('optional') and not c.get('has_hint') and not prev.get('has_hint'):
                prev['graphs'] = dict(nodes=_set(prev['graphs']['nodes'] + c['graphs']['nodes']),
                                      rels=_set(prev['graphs']['rels'] + c['graphs']['rels']))
                prev['predicate'] = _set(prev['predicate'] + c['predicate'])
                continue
            reading.append(c)
        out = dict(updating=len(p.get('updating') or []), reading=reading,
                   projection=self.projection(p.get('projection')))
        self.scope = self.aliases(p.get('projection'))
        out['predicate'] = self.expr(p.get('predicate'))
        return out

    def single(self, q):
        self.scope = {}
        return [self.part(p) for p in q.get('parts') or []]

    def statement(self, s):
        return dict(statement_type=s.get('statement_type'),
                    single_queries=[self.single(q) for q in s.get('single_queries') or []],
                    union_all=list(s.get('union_all') or []),
                    pre_parts=len(s.get('pre_parts') or []), post_query=s.get('post_query'))


def raw_names(statement):
    """uid -> raw name for a statement bound from printed Cypher (every entity
    was printed under an explicit, unique variable name)."""
    names = {}

    def walk(value):
        if isinstance(value, dict):
            if value.get('kind') in ('NODE', 'REL') and 'tables' in value and 'uid' in value:
                names.setdefault(value['uid'], value.get('raw'))
            elif _is_entity(value):
                names.setdefault(value['entity'], value.get('raw'))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(statement)
    return {uid: raw for uid, raw in names.items() if raw}


def canonical(statement, names, rel_pairs=None):
    return Canonicalizer(names, rel_pairs).statement(statement)


def rel_pairs_of(schema):
    pairs = {}
    for edge in schema.edges:
        for pair in edge.get('vertex_type_pair_relations') or []:
            child = f"{edge['type_name']}_{pair['source_vertex']}_{pair['destination_vertex']}"
            pairs[child] = (pair['source_vertex'], pair['destination_vertex'])
            pairs.setdefault(edge['type_name'], None)
    return {k: v for k, v in pairs.items() if v is not None}


def first_difference(a, b, path='$'):
    if type(a) is not type(b):
        return f'{path}: {json.dumps(a)[:160]} != {json.dumps(b)[:160]}'
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f'{path}.{key}: present only on one side'
            diff = first_difference(a[key], b[key], f'{path}.{key}')
            if diff:
                return diff
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f'{path}: length {len(a)} != {len(b)}'
        for i, (x, y) in enumerate(zip(a, b)):
            diff = first_difference(x, y, f'{path}[{i}]')
            if diff:
                return diff
        return None
    return None if a == b else f'{path}: {json.dumps(a)[:160]} != {json.dumps(b)[:160]}'
