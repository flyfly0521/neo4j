"""Print Bound IR v2 (engine/bound_ir.h) as Cypher for a target dialect.

Strict by design: a construct whose meaning cannot be reproduced exactly
raises Unsupported and the middleware returns the original query.  Output is
additionally verified by re-binding (canonical.py), so the printer is never
trusted blindly.
"""
from dataclasses import dataclass, field

from .dialects import Dialect, get_dialect
from .errors import Unsupported

INTERNAL_ID = '_ID'
WALK = 1  # QueryRelType::VARIABLE_LENGTH_WALK


@dataclass
class Printed:
    cypher: str
    entity_names: dict = field(default_factory=dict)  # entity uid -> printed variable name
    parts: list = field(default_factory=list)         # QueryPart texts (single query only)


def is_entity_ref(e):
    return e is not None and 'entity' in e


def children_of(e):
    out = list(e.get('children') or [])
    for alt in e.get('alternatives') or []:
        out += [alt.get('when'), alt.get('then')]
    if e.get('else') is not None:
        out.append(e['else'])
    return [c for c in out if c is not None]


def contains_aggregate(e, scope=()):
    """True when `e` aggregates here.  References to columns projected by an
    earlier WITH (uids in `scope`) are plain values, even if they were
    defined by an aggregate."""
    if e is None or e.get('uid') in scope:
        return False
    return e.get('k') == 'AGGREGATE_FUNCTION' or any(contains_aggregate(c, scope) for c in children_of(e))


def iter_entities(value):
    """Yield graph entities (uid, raw, kind) in deterministic order."""
    if isinstance(value, dict):
        if value.get('kind') in ('NODE', 'REL') and 'tables' in value and 'uid' in value:
            yield value['uid'], value.get('raw', ''), value['kind']
        elif is_entity_ref(value):
            yield value['entity'], value.get('raw', ''), value.get('entity_kind', 'NODE')
        for child in value.values():
            yield from iter_entities(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_entities(child)


class Printer:
    def __init__(self, dialect=None):
        self.dialect = dialect if isinstance(dialect, Dialect) else get_dialect(dialect or 'neug')

    # ------------------------------------------------------------- naming
    def _assign_names(self, statement):
        taken = set()

        def collect(value):
            if isinstance(value, dict):
                if 'names' in value and isinstance(value['names'], list):
                    taken.update(value['names'])
                if value.get('kind') == 'UNWIND' and value.get('out'):
                    taken.add(value['out'].get('name', ''))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(statement)
        entities, seen = [], set()
        for uid, raw, kind in iter_entities(statement):
            if uid not in seen:
                seen.add(uid)
                entities.append((uid, raw, kind))
        taken.update(raw for _, raw, _ in entities if raw)
        names, used, counter = {}, set(), 0
        for uid, raw, kind in entities:
            # A user-written name can repeat only across disjoint scopes (e.g.
            # `WITH collect(z) AS zs UNWIND zs AS z`), so it is kept as is.
            name = raw
            if not name:
                base = name or ('cico_n' if kind == 'NODE' else 'cico_r')
                while True:
                    counter += 1
                    name = f'{raw}_cico{counter}' if raw else f'{base}{counter}'
                    if name not in taken and name not in used:
                        break
            used.add(name)
            names[uid] = name
        return names

    # ---------------------------------------------------------- statement
    def print(self, statement, final_with=False):
        """final_with: end with `WITH ...` instead of `RETURN ...` so that a
        caller can append clauses (used to build index materializations)."""
        self.final_with = final_with
        if not statement or statement.get('unsupported') or statement.get('statement_type') != 0:
            raise Unsupported('non-query statement')
        if statement.get('pre_parts') or statement.get('pre_expressions') or statement.get('post_query'):
            raise Unsupported('pre/post query parts')
        self.names = self._assign_names(statement)
        tables = {}

        def collect(value):
            if isinstance(value, dict):
                if value.get('kind') in ('NODE', 'REL') and 'tables' in value and 'uid' in value:
                    tables.setdefault(value['uid'], value.get('tables') or [])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(statement)
        # Dialects with schema mappings (Neo4j) need entity tables and names.
        self.dialect.entity_tables = tables
        self.dialect.printer_names = self.names
        singles = statement.get('single_queries') or []
        if not singles:
            raise Unsupported('empty query')
        texts = [self._single(s) for s in singles]
        flags = statement.get('union_all') or []
        if final_with and len(texts) > 1:
            raise Unsupported('UNION source cannot be continued')
        if len(flags) != len(texts) - 1:
            raise Unsupported('union flags')
        out = texts[0]
        for flag, text in zip(flags, texts[1:]):
            out += (' UNION ALL ' if flag else ' UNION ') + text
        parts = self.part_texts if len(texts) == 1 else []
        return Printed(self.dialect.finish(out), dict(self.names), parts)

    def _single(self, single):
        parts = single.get('parts') or []
        if not parts:
            raise Unsupported('query without parts')
        self.alias_of = {}
        self.declared = set()
        self.part_texts = [self._part(p, i == len(parts) - 1 and not getattr(self, 'final_with', False))
                           for i, p in enumerate(parts)]
        return ' '.join(self.part_texts)

    def _part(self, part, last):
        if part.get('updating'):
            raise Unsupported('updating clauses')
        out = [self._clause(c) for c in part.get('reading') or []]
        if part.get('projection') is None:
            raise Unsupported('query part without projection')
        out.append(self._projection(part['projection'], part.get('predicate'), last))
        return ' '.join(out)

    def _clause(self, clause):
        kind = clause.get('kind')
        if kind == 'MATCH':
            if clause.get('has_hint'):
                raise Unsupported('join hint')
            self.label_override, predicate = self.dialect.fold_node_labels(
                clause.get('graphs'), clause.get('predicate'), self.declared)
            text = self.dialect.match_text('OPTIONAL MATCH' if clause.get('optional') else 'MATCH',
                                           self._pattern_parts(clause.get('graphs')), predicate)
            self.label_override = {}
        elif kind == 'UNWIND':
            out = clause.get('out') or {}
            if is_entity_ref(out):
                # UNWIND of a node list binds a graph entity.
                name = self.names.get(out['entity'])
                self.declared.add(out['entity'])
            else:
                name = out.get('name')
            if not name:
                raise Unsupported('UNWIND without variable')
            text = f"UNWIND {self.expr(clause['in'])} AS {self.dialect.ident(name)}"
        else:
            raise Unsupported(f"reading clause type {clause.get('clause_type')}")
        extra = self.dialect.pending_predicates()
        if kind != 'MATCH':
            predicate = clause.get('predicate')
        conditions = ([self.expr(predicate)] if predicate is not None else []) + extra
        if conditions:
            if kind != 'MATCH':
                raise Unsupported('predicate on UNWIND')
            text += ' WHERE ' + ' AND '.join(conditions)
        return text

    # ----------------------------------------------------------- patterns
    def _node(self, node):
        name = self.names[node['uid']]
        if node['uid'] in self.declared:
            return f'({self.dialect.ident(name)})'
        self.declared.add(node['uid'])
        label = (getattr(self, 'label_override', None) or {}).get(node['uid'])
        if label is not None:
            return f'({self.dialect.ident(name)}:{self.dialect.ident(label)})'
        return '(' + self.dialect.node_pattern(name, node.get('tables') or []) + ')'

    def _rel(self, rel):
        name = self.dialect.ident(self.names[rel['uid']])
        self.declared.add(rel['uid'])
        types = self.dialect.rel_types(rel.get('tables') or [])
        recursive = rel.get('recursive')
        if recursive is None:
            if rel.get('rel_type', 0) != 0:
                raise Unsupported('recursive relationship without bounds')
            return f'[{name}{types}]'
        if recursive.get('unsupported') or rel.get('rel_type') != WALK or recursive.get('semantic', 0) != 0:
            raise Unsupported('non-walk recursive relationship')
        if recursive.get('node_predicate') or recursive.get('rel_predicate') or recursive.get('weighted'):
            raise Unsupported('recursive relationship with predicates')
        return f"[{name}{types}{self.dialect.var_length(recursive['lower'], recursive['upper'])}]"

    def _graphs(self, graphs):
        return ', '.join(text for text, _ in self._pattern_parts(graphs))

    def _pattern_parts(self, graphs):
        """[(pattern text, relationship IR or None)], one per relationship
        plus one per isolated node."""
        if not graphs:
            raise Unsupported('MATCH without graphs')
        segments = []
        for graph in graphs:
            nodes = {n['uid']: n for n in graph.get('nodes') or []}
            touched = set()
            for rel in graph.get('rels') or []:
                if rel['src'] not in nodes or rel['dst'] not in nodes:
                    raise Unsupported('relationship endpoint outside its query graph')
                touched.update((rel['src'], rel['dst']))
                body = self._rel(rel)
                src, dst = self._node(nodes[rel['src']]), self._node(nodes[rel['dst']])
                if rel.get('direction') == 'SINGLE':
                    segments.append((f'{src}-{body}->{dst}', rel))
                elif rel.get('direction') == 'BOTH':
                    segments.append((f'{src}-{body}-{dst}', rel))
                else:
                    raise Unsupported('relationship direction')
            for uid, node in nodes.items():
                if uid not in touched:
                    segments.append((self._node(node), None))
        return segments

    # --------------------------------------------------------- projection
    def _projection(self, projection, predicate, last):
        expressions = projection.get('expressions') or []
        names = projection.get('names') or []
        if not names and expressions and all(is_entity_ref(e) for e in expressions):
            # Binder-synthesised WITH (e.g. the variadic anchor split's
            # `WITH DISTINCT message`) carries no names; entities keep theirs.
            names = [self.names.get(e['entity']) for e in expressions]
        if not expressions or len(names) != len(expressions):
            raise Unsupported('projection names')
        self._check_grouping(projection)
        items, new_alias = [], {}
        for e, name in zip(expressions, names):
            if is_entity_ref(e):
                if self.names.get(e['entity']) != name:
                    raise Unsupported('graph entity projected under a different name')
                items.append(self.dialect.ident(name))
                continue
            text = self.expr(e)
            if not name:
                # Un-aliased item: the binder derives the column name itself
                # (verified through the output column contract).
                items.append(text)
                continue
            items.append(text if text == self.dialect.ident(name) else f'{text} AS {self.dialect.ident(name)}')
            new_alias[e['uid']] = name
        out = ('RETURN' if last else 'WITH') + (' DISTINCT ' if projection.get('distinct') else ' ') + ', '.join(items)
        order_by, ascending = projection.get('order_by') or [], projection.get('ascending') or []
        if len(order_by) != len(ascending):
            raise Unsupported('sorting orders')
        if order_by:
            saved = self.alias_of
            self.alias_of = {**saved, **new_alias}
            out += ' ORDER BY ' + ', '.join(self.expr(e) + ('' if asc else ' DESC')
                                            for e, asc in zip(order_by, ascending))
            self.alias_of = saved
        if projection.get('skip') is not None:
            out += ' SKIP ' + self.expr(projection['skip'])
        if projection.get('limit') is not None:
            out += ' LIMIT ' + self.expr(projection['limit'])
        self.alias_of = new_alias
        if predicate is not None:
            if last:
                raise Unsupported('predicate after RETURN')
            out += ' WHERE ' + self.expr(predicate)
        return out

    def _check_grouping(self, projection):
        scope = self.alias_of
        expressions = projection.get('expressions') or []
        aggregating = any(contains_aggregate(e, scope) for e in expressions)
        group_by = {e['uid'] for e in projection.get('group_by') or []}
        if not aggregating:
            if group_by:
                raise Unsupported('grouping keys without aggregation')
            return
        implicit = {e['uid'] for e in expressions if not contains_aggregate(e, scope)}
        if group_by != implicit:
            raise Unsupported('explicit grouping keys differ from implicit Cypher grouping')

    # -------------------------------------------------------- expressions
    def entity_name(self, uid):
        if uid in self.alias_of:
            return self.alias_of[uid]
        if uid not in self.names:
            raise Unsupported('reference to an unknown graph entity')
        return self.names[uid]

    def expr(self, e):
        if e is None:
            raise Unsupported('missing expression')
        uid = e.get('uid')
        if uid in self.alias_of and not is_entity_ref(e):
            return self.dialect.ident(self.alias_of[uid])
        kind = e.get('k')
        if e.get('unsupported'):
            raise Unsupported(f'expression {kind}')
        if is_entity_ref(e):
            return self.dialect.ident(self.entity_name(e['entity']))
        if kind == 'PROPERTY':
            owner = self.entity_name(e['owner']) if (e['owner'] in self.names or e['owner'] in self.alias_of) \
                else None
            if owner is None:
                raise Unsupported('property of an unknown owner')
            if e['property'] == INTERNAL_ID:
                # `count(x)`, `x IS NULL`, `x = y` bind to the internal ID.
                return self.dialect.ident(owner)
            return self.dialect.property(owner, e['property'], e['owner'])
        if kind == 'LITERAL':
            return self.dialect.literal(e['value'])
        if kind == 'PARAMETER':
            return self.dialect.parameter(e['name'])
        if kind == 'VARIABLE':
            if not e.get('name'):
                raise Unsupported('anonymous variable')
            return self.dialect.ident(e['name'])
        if kind == 'CASE_ELSE':
            alternatives = e.get('alternatives') or []
            if not alternatives:
                raise Unsupported('CASE without alternatives')
            text = 'CASE' + ''.join(f" WHEN {self.expr(a['when'])} THEN {self.expr(a['then'])}" for a in alternatives)
            if e.get('else') is not None:
                text += ' ELSE ' + self.expr(e['else'])
            return text + ' END'
        if 'subquery' in e:
            return self._subquery(e)
        children = e.get('children') or []
        special = self.dialect.special_expression(e, self)
        if special is not None:
            return special
        if kind == 'AGGREGATE_FUNCTION':
            return self.dialect.aggregate(e['function'], [self.expr(c) for c in children], e.get('distinct', False))
        function = e.get('function', kind)
        if function == 'LABEL' and children and children[0].get('k') == 'PROPERTY' \
                and children[0].get('property') == INTERNAL_ID:
            return self.dialect.label_of(self.expr(children[0]))
        return self.dialect.function(function, [self.expr(c) for c in children], e)

    def _subquery(self, e):
        if e.get('has_hint') or e.get('subquery') not in ('EXISTS', 'COUNT'):
            raise Unsupported('subquery shape')
        text = f"{e['subquery']} {{ {self.dialect.match_text('MATCH', self._pattern_parts(e.get('graphs')), e.get('where'))}"
        conditions = ([self.expr(e['where'])] if e.get('where') is not None else []) + self.dialect.pending_predicates()
        if conditions:
            text += ' WHERE ' + ' AND '.join(conditions)
        return text + ' }'


def print_statement(statement, dialect='neug'):
    return Printer(dialect).print(statement)
