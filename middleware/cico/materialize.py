"""Index materialization for the target system (M2): FILL -> statements.

For each registered FILL the materializer emits, in the profile's dialect:

  ddl     carrier columns / derived relationship tables (Kuzu; Neo4j: none)
  reset   carrier values back to the column DEFAULT (or derived edges removed),
          so a build can be repeated and Neo4j nodes outside the FILL result
          read the same default as in NeuG
  fill    the FILL source query printed in the target dialect, ending in
          `WITH <key entities>, <values>` followed by SET / CREATE

Key columns that are the primary key of a FILL endpoint are replaced by the
endpoint entity itself, so no lookup by id is needed (and stores without an id
property, such as Neo4j LSQB, work).  Other keys are matched by primary key.

FILL kinds (as in NeuG):
  T(c1..cn) on a node table       source RETURN key, v1..vn          -> SET n.ci
  R(c1..cn) on an existing rel    source RETURN src, dst, v1..vn     -> SET e.ci
  R() on a derived rel table      source RETURN src, dst, p1..pk     -> CREATE edge
"""
import copy

from .errors import Unsupported
from .printer import Printer
from .schema import type_name
from .statements import parse_fill

ALIAS = 'cico_v'


def _upper(names):
    return {n.upper() for n in names or []}


class Materializer:
    def __init__(self, middleware, include_internal_state=False):
        self.mw = middleware
        self.include_internal_state = include_internal_state
        self._types = {}

    # ------------------------------------------------------------ helpers
    def _table(self, name):
        kind, item = self.mw.schema.table(name)
        if item is None:
            raise Unsupported(f'unknown FILL target table {name}')
        return kind, item

    def _group_of(self, name):
        """Child relationship table (GROUP_SRC_DST) -> (group item, pair)."""
        for edge in self.mw.schema.edges:
            for pair in edge.get('vertex_type_pair_relations') or []:
                child = f"{edge['type_name']}_{pair['source_vertex']}_{pair['destination_vertex']}"
                if child.upper() == name.upper():
                    return edge, pair
        return None, None

    def _default(self, table, column):
        if self.include_internal_state and column.startswith('__neug_fill_state_'):
            return '0'
        return self.mw.schema.defaults.get((table.upper(), column))

    def _key_entity(self, expression, entities, tables):
        """Entity uid when `expression` is `x.<pk>` of a node over `tables`."""
        if expression.get('entity') in entities:
            node = entities[expression['entity']]
            if node.get('kind') == 'NODE' and node.get('tables') and _upper(node['tables']) <= _upper(tables):
                return expression['entity']
        if expression.get('k') != 'PROPERTY':
            return None
        owner = expression.get('owner')
        node = entities.get(owner)
        if node is None or node.get('kind') != 'NODE' or not node.get('tables'):
            return None
        if not _upper(node['tables']) <= _upper(tables):
            return None
        for table in node['tables']:
            _, item = self.mw.schema.table(table)
            if item is None or expression.get('property') not in (item.get('primary_keys') or []):
                return None
        return owner

    # ---------------------------------------------------------------- API
    def statements(self, fill_text):
        """{'ddl': [...], 'reset': [...], 'fill': [...]} for one FILL (internal text)."""
        fill = parse_fill(fill_text)
        if fill is None:
            raise Unsupported('cannot parse FILL')
        d = self.mw.profile.dialect
        kind, item = self.mw.schema.table(fill.table)
        child = None
        if item is None:  # FILL into one child of a REL group (q5_index_Post_Tag)
            item, child = self._group_of(fill.table)
            kind = 'REL'
            if item is None:
                raise Unsupported(f'unknown FILL target table {fill.table}')
        bound = (self.mw.engine.request('describe_fill', fill=fill_text) if self.include_internal_state
                 else self.mw.engine.bind(fill.source))
        ir = copy.deepcopy(bound['ir'])
        singles = ir.get('single_queries') or []
        if len(singles) != 1:
            raise Unsupported('UNION FILL source')
        projection = singles[0]['parts'][-1]['projection']
        expressions = projection['expressions']
        entities = {}

        def collect(value):
            if isinstance(value, dict):
                if value.get('kind') in ('NODE', 'REL') and 'uid' in value and 'tables' in value:
                    entities.setdefault(value['uid'], value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(ir)

        if kind == 'NODE':
            keys = [[item['type_name']]]
            values = list(fill.columns)
        else:
            pairs = [child] if child else item.get('vertex_type_pair_relations') or []
            keys = [[p['source_vertex'] for p in pairs], [p['destination_vertex'] for p in pairs]]
            derived = not fill.columns
            values = list(fill.columns) if not derived else \
                [p['property_name'] for p in item.get('properties') or []][:len(expressions) - 2]
        if self.include_internal_state and bound.get('target_columns'):
            values = list(bound['target_columns'])
        self._types = {c: e.get('type') for c, e in zip(values, expressions[len(keys):])}
        if len(expressions) != len(keys) + len(values):
            raise Unsupported(f'FILL source returns {len(expressions)} columns, expected '
                              f'{len(keys)} keys + {len(values)} values')

        # Final projection: keys become entities (or stay ids), values get aliases.
        key_names, key_lookup = [], []
        names = list(projection.get('names') or [''] * len(expressions))
        names += [''] * (len(expressions) - len(names))
        if self.include_internal_state and projection.get('group_by'):
            # Compatibility FILLs append a literal 1 as a per-group presence
            # flag. Adding a constant to already nonempty grouping keys does
            # not split/merge groups, and makes implicit Cypher grouping agree.
            grouped = {e['uid'] for e in projection['group_by']}
            projection['group_by'] += [e for e in expressions if e.get('k') == 'LITERAL'
                                       and e.get('uid') not in grouped]
        for i, tables in enumerate(keys):
            uid = self._key_entity(expressions[i], entities, tables)
            old = expressions[i]
            if uid is not None:
                ref = dict(k='PATTERN', uid=uid, entity=uid, entity_kind='NODE',
                           raw=entities[uid].get('raw', ''), type='NODE')
                expressions[i] = ref
                for collection in ('group_by',):
                    projection[collection] = [ref if e.get('uid') == old.get('uid') else e
                                              for e in projection.get(collection) or []]
                key_names.append(uid)
                key_lookup.append(None)
            else:
                names[i] = f'cico_k{i}'
                key_names.append(None)
                key_lookup.append(names[i])
        for j in range(len(values)):
            names[len(keys) + j] = f'{ALIAS}{j}'
        # Entity keys are projected under their own variable name; anonymous
        # endpoints get a stable one first.
        for i, uid in enumerate(key_names):
            if uid is not None and not entities[uid].get('raw'):
                self._rename(ir, uid, f'cico_key{i}')
        printer = Printer(d)
        for i, uid in enumerate(key_names):
            if uid is not None:
                names[i] = entities[uid].get('raw') or f'cico_key{i}'
        projection['names'] = names
        body = printer.print(ir, final_with=True).cypher

        def endpoint(i, var):
            if key_names[i] is not None:
                return f'({d.ident(printer.names[key_names[i]])})', ''
            table = keys[i][0] if len(keys[i]) == 1 else None
            if table is None:
                raise Unsupported('id key over several endpoint tables')
            _, t = self._table(table)
            pk = (t.get('primary_keys') or [None])[0]
            if pk is None:
                raise Unsupported(f'{table} has no primary key')
            return (f'({d.ident(var)})',
                    f'MATCH ({d.node_pattern(var, [table])} {{{d.ident(pk)}: {d.ident(key_lookup[i])}}}) ')

        out = dict(ddl=[], reset=[], fill=[])
        target = item['type_name']
        if kind == 'NODE':
            node, lookup = endpoint(0, 'cico_t')
            sets = ', '.join(f'{node[1:-1]}.{d.ident(c)} = {d.ident(ALIAS + str(j))}' for j, c in enumerate(values))
            out['fill'].append(d.fill_statement(body, f'{lookup}SET {sets}', names))
            for c in values:
                out['ddl'] += d.add_column_ddl(target, c, self._column_type(item, c), self._default(target, c))
                default = self._default(target, c)
                out['reset'].append(d.fill_statement(
                    f'MATCH ({d.node_pattern("cico_n", [target])}) WITH cico_n',
                    f'SET cico_n.{d.ident(c)} = {default if default is not None else "NULL"}', ['cico_n']))
        else:
            src, src_lookup = endpoint(0, 'cico_s')
            dst, dst_lookup = endpoint(1, 'cico_d')
            types = d.rel_types([target])
            if not derived:
                sets = ', '.join(f'cico_e.{d.ident(c)} = {d.ident(ALIAS + str(j))}' for j, c in enumerate(values))
                out['fill'].append(d.fill_statement(
                    body, f'{src_lookup}{dst_lookup}MATCH {src}-[cico_e{types}]->{dst} SET {sets}', names))
                for c in values:
                    out['ddl'] += d.add_column_ddl(target, c, self._column_type(item, c), self._default(target, c))
                    default = self._default(target, c)
                    out['reset'].append(d.fill_statement(
                        f'MATCH ()-[cico_e{types}]->() WITH cico_e',
                        f'SET cico_e.{d.ident(c)} = {default if default is not None else "NULL"}', ['cico_e']))
            else:
                props = ', '.join(f'{d.ident(c)}: {d.ident(ALIAS + str(j))}' for j, c in enumerate(values))
                props = f' {{{props}}}' if props else ''
                union = [i for i in (0, 1) if key_names[i] is not None
                         and len(entities[key_names[i]].get('tables') or []) > 1]
                if union and not d.create_on_label_union:
                    # One statement per child pair: filter the source rows by the
                    # endpoints' labels and rebind each endpoint under one label.
                    for pair in pairs:
                        labels = (pair['source_vertex'], pair['destination_vertex'])
                        conditions, rebinds, ends = [], [], []
                        for i in (0, 1):
                            var = d.ident(names[i])
                            if i in union:
                                own = [t for t in entities[key_names[i]]['tables'] if t.upper() == labels[i].upper()]
                                if not own:
                                    break
                                _, t = self._table(labels[i])
                                pk = (t.get('primary_keys') or [None])[0]
                                if pk is None:
                                    raise Unsupported(f'{labels[i]} has no primary key')
                                alias = f'cico_e{i}'
                                conditions.append(f"label({var}) = '{labels[i]}'")
                                rebinds.append(f'MATCH ({d.node_pattern(alias, [labels[i]])} '
                                               f'{{{d.ident(pk)}: {var}.{d.ident(pk)}}}) ')
                                ends.append(f'({alias})')
                            else:
                                ends.append((src, dst)[i])
                        else:
                            tail = (f"WITH {', '.join(d.ident(n) for n in names)} WHERE {' AND '.join(conditions)} "
                                    + ''.join(rebinds) + f'CREATE {ends[0]}-[{types}{props}]->{ends[1]}')
                            out['fill'].append(d.fill_statement(body, tail, names, eager=True))
                else:
                    out['fill'].append(d.fill_statement(
                        body, f'{src_lookup}{dst_lookup}CREATE {src}-[{types}{props}]->{dst}', names, eager=True,
                        partition=self._partition(ir, key_names, names, entities)))
                out['ddl'] += d.create_rel_table_ddl(item, self.mw.schema)
                out['reset'].append(d.fill_statement(f'MATCH ()-[cico_e{types}]->() WITH cico_e',
                                                     'DELETE cico_e', ['cico_e']))
        return out

    @staticmethod
    def _partition(ir, key_names, names, entities):
        """(variable, tables) of a key node by which the source rows can be
        partitioned: every projection of the source keeps it as a key and
        none orders / limits.  Running the source once per such node and
        unioning the results reproduces the whole result."""
        parts = ir['single_queries'][0]['parts']
        for i, uid in enumerate(key_names):
            if uid is None:
                continue
            ok = True
            for part in parts:
                p = part.get('projection') or {}
                if p.get('order_by') or p.get('skip') is not None or p.get('limit') is not None:
                    ok = False
                if not any(e.get('entity') == uid for e in p.get('expressions') or []):
                    ok = False
            if ok:
                return names[i], entities[uid].get('tables') or []
        return None

    @staticmethod
    def _rename(value, uid, raw):
        if isinstance(value, dict):
            if value.get('uid') == uid or value.get('entity') == uid:
                value['raw'] = raw
            for child in value.values():
                Materializer._rename(child, uid, raw)
        elif isinstance(value, list):
            for child in value:
                Materializer._rename(child, uid, raw)

    def _column_type(self, item, column):
        for p in item.get('properties') or []:
            if p['property_name'] == column:
                return type_name(p['property_type'])
        if self.include_internal_state and column in self._types:
            return self._types[column]
        raise Unsupported(f'unknown carrier column {item["type_name"]}.{column}')
