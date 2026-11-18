"""The middleware's own copy of the graph schema (NeuG schema YAML).

DDL statements sent to the middleware (CREATE NODE/REL TABLE, ALTER TABLE
ADD/DROP, DROP TABLE) are applied here; the engine catalogs are then reloaded
from the YAML.  The target system's real schema is not touched: the
middleware only needs to know which carrier tables/columns exist.
"""
import copy
import re

import yaml

PRIMITIVE = {
    'INT64': 'DT_SIGNED_INT64', 'INT': 'DT_SIGNED_INT64', 'SERIAL': 'DT_SIGNED_INT64',
    'INT32': 'DT_SIGNED_INT32', 'UINT64': 'DT_UNSIGNED_INT64', 'UINT32': 'DT_UNSIGNED_INT32',
    'DOUBLE': 'DT_DOUBLE', 'FLOAT': 'DT_FLOAT', 'BOOL': 'DT_BOOL', 'BOOLEAN': 'DT_BOOL',
}
TEMPORAL = {'DATE': 'date', 'TIMESTAMP': 'datetime', 'DATETIME': 'datetime', 'INTERVAL': 'interval'}
REVERSE_PRIMITIVE = {v: k for k, v in PRIMITIVE.items() if k in ('INT64', 'INT32', 'UINT64', 'UINT32',
                                                                 'DOUBLE', 'FLOAT', 'BOOL')}


class SchemaError(ValueError):
    pass


def property_type(type_name):
    """NeuG logical type name (as printed by the binder, e.g. INT64, STRING) -> YAML."""
    name = type_name.strip().upper()
    if name in PRIMITIVE:
        return {'primitive_type': PRIMITIVE[name]}
    if name == 'STRING' or name.startswith('VARCHAR'):
        return {'string': {'var_char': {'max_length': 256}}}
    if name.startswith('TIMESTAMP') or name in TEMPORAL:
        return {'temporal': {TEMPORAL.get(name, 'datetime'): ''}}
    raise SchemaError(f'Unsupported property type for the middleware schema: {type_name}')


def type_name(yaml_type):
    if 'primitive_type' in yaml_type:
        return REVERSE_PRIMITIVE.get(yaml_type['primitive_type'], yaml_type['primitive_type'])
    if 'string' in yaml_type:
        return 'STRING'
    if 'temporal' in yaml_type:
        kind = next(iter(yaml_type['temporal']))
        return {'date': 'DATE', 'datetime': 'TIMESTAMP', 'interval': 'INTERVAL'}.get(kind, kind.upper())
    return str(yaml_type)


_IDENT = r'(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)'


def _name(token):
    token = token.strip()
    return token[1:-1] if token.startswith('`') else token


def _split_top(text):
    parts, depth, current = [], 0, ''
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return [p.strip() for p in parts]


def _normalize(node):
    """Storage-side graph.yaml spells string types `varchar`; the compiler
    metadata (and the engine) read `var_char`."""
    if isinstance(node, dict):
        return {('var_char' if k == 'varchar' else k): _normalize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    return node


class Schema:
    def __init__(self, document, defaults=None):
        self.doc = document
        # (TABLE, column) -> DEFAULT literal (internal Cypher) from DDL; the
        # NeuG YAML has no slot for it, the materializer needs it.
        self.defaults = dict(defaults or {})
        self.doc.setdefault('schema', {}).setdefault('vertex_types', [])
        self.doc['schema'].setdefault('edge_types', [])

    @classmethod
    def from_text(cls, text):
        return cls(_normalize(yaml.safe_load(text) or {}))

    @classmethod
    def from_file(cls, path):
        with open(path) as handle:
            return cls.from_text(handle.read())

    def copy(self):
        return Schema(copy.deepcopy(self.doc), self.defaults)

    def to_yaml(self):
        return yaml.safe_dump(self.doc, sort_keys=False)

    # ---------------------------------------------------------------- lookup
    @property
    def vertices(self):
        return self.doc['schema']['vertex_types']

    @property
    def edges(self):
        return self.doc['schema']['edge_types']

    def table(self, name):
        for kind, items in (('NODE', self.vertices), ('REL', self.edges)):
            for item in items:
                if item['type_name'].upper() == name.upper():
                    return kind, item
        return None, None

    def properties(self, name):
        _, item = self.table(name)
        if item is None:
            raise SchemaError(f'Unknown table {name}')
        return {p['property_name']: type_name(p['property_type']) for p in item.get('properties') or []}

    def has_property(self, table, prop):
        return prop in self.properties(table)

    def node_tables(self):
        return [v['type_name'] for v in self.vertices]

    # ------------------------------------------------------------- mutation
    def _next_type_id(self, items):
        return max([i.get('type_id', -1) for i in items] + [-1]) + 1

    def add_property(self, table, prop, type_text, if_not_exists=False):
        _, item = self.table(table)
        if item is None:
            raise SchemaError(f'Unknown table {table}')
        props = item.setdefault('properties', []) or []
        item['properties'] = props
        if any(p['property_name'] == prop for p in props):
            if if_not_exists:
                return False
            raise SchemaError(f'Property {table}.{prop} already exists')
        next_id = max([p.get('property_id', -1) for p in props] + [-1]) + 1
        props.append(dict(property_id=next_id, property_name=prop, property_type=property_type(type_text)))
        return True

    def drop_property(self, table, prop, if_exists=False):
        _, item = self.table(table)
        if item is None:
            raise SchemaError(f'Unknown table {table}')
        before = len(item.get('properties') or [])
        item['properties'] = [p for p in item.get('properties') or [] if p['property_name'] != prop]
        if before == len(item['properties']) and not if_exists:
            raise SchemaError(f'Property {table}.{prop} does not exist')
        return before != len(item['properties'])

    def create_node_table(self, name, columns, primary_key, if_not_exists=False):
        if self.table(name)[1] is not None:
            if if_not_exists:
                return False
            raise SchemaError(f'Table {name} already exists')
        props = [dict(property_id=i, property_name=c, property_type=property_type(t))
                 for i, (c, t) in enumerate(columns)]
        self.vertices.append(dict(type_name=name, type_id=self._next_type_id(self.vertices),
                                  properties=props, primary_keys=[primary_key]))
        return True

    def create_rel_table(self, name, pairs, columns, multiplicity='MANY_TO_MANY', if_not_exists=False):
        if self.table(name)[1] is not None:
            if if_not_exists:
                return False
            raise SchemaError(f'Table {name} already exists')
        canonical = {n.upper(): n for n in self.node_tables()}
        relations = []
        for src, dst in pairs:
            if src.upper() not in canonical or dst.upper() not in canonical:
                raise SchemaError(f'Unknown endpoint table in {name}: {src} -> {dst}')
            relations.append(dict(source_vertex=canonical[src.upper()], destination_vertex=canonical[dst.upper()],
                                  relation=multiplicity))
        props = [dict(property_id=i, property_name=c, property_type=property_type(t))
                 for i, (c, t) in enumerate(columns)]
        self.edges.append(dict(type_name=name, type_id=self._next_type_id(self.edges), properties=props,
                               vertex_type_pair_relations=relations))
        return True

    def drop_table(self, name, if_exists=False):
        kind, item = self.table(name)
        if item is None:
            if if_exists:
                return False
            raise SchemaError(f'Unknown table {name}')
        (self.vertices if kind == 'NODE' else self.edges).remove(item)
        return True

    # ------------------------------------------------------------------ DDL
    MULTIPLICITY = {'MANY_ONE': 'MANY_TO_ONE', 'ONE_MANY': 'ONE_TO_MANY', 'ONE_ONE': 'ONE_TO_ONE',
                    'MANY_MANY': 'MANY_TO_MANY'}

    def apply_ddl(self, statement):
        """Apply one DDL statement.  Returns True when the schema changed."""
        s = statement.strip().rstrip(';').strip()
        m = re.fullmatch(rf'ALTER\s+TABLE\s+({_IDENT})\s+ADD\s+(IF\s+NOT\s+EXISTS\s+)?({_IDENT})\s+'
                         rf'([A-Za-z0-9_]+(?:\([^)]*\))?)(?:\s+DEFAULT\s+(.+))?', s, re.I | re.S)
        if m:
            changed = self.add_property(_name(m[1]), _name(m[3]), m[4], bool(m[2]))
            if changed and m[5]:
                self.defaults[(_name(m[1]).upper(), _name(m[3]))] = m[5].strip()
            return changed
        m = re.fullmatch(rf'ALTER\s+TABLE\s+({_IDENT})\s+DROP\s+(IF\s+EXISTS\s+)?({_IDENT})', s, re.I)
        if m:
            return self.drop_property(_name(m[1]), _name(m[3]), bool(m[2]))
        m = re.fullmatch(rf'DROP\s+TABLE\s+(IF\s+EXISTS\s+)?({_IDENT})', s, re.I)
        if m:
            return self.drop_table(_name(m[2]), bool(m[1]))
        m = re.fullmatch(rf'CREATE\s+(NODE|REL)\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?({_IDENT})\s*\((.*)\)', s, re.I | re.S)
        if m:
            kind, if_not_exists, name, body = m[1].upper(), bool(m[2]), _name(m[3]), m[4]
            columns, pairs, primary_key, multiplicity = [], [], None, 'MANY_TO_MANY'
            for item in _split_top(body):
                pk = re.fullmatch(rf'PRIMARY\s+KEY\s*\(\s*({_IDENT})\s*\)', item, re.I)
                pair = re.fullmatch(rf'FROM\s+({_IDENT})\s+TO\s+({_IDENT})', item, re.I)
                if pk:
                    primary_key = _name(pk[1])
                elif pair:
                    pairs.append((_name(pair[1]), _name(pair[2])))
                elif item.upper() in self.MULTIPLICITY:
                    multiplicity = self.MULTIPLICITY[item.upper()]
                else:
                    col = re.fullmatch(rf'({_IDENT})\s+([A-Za-z0-9_]+(?:\([^)]*\))?)(\s+PRIMARY\s+KEY)?(?:\s+DEFAULT\s+(.+))?',
                                       item, re.I | re.S)
                    if not col:
                        raise SchemaError(f'Cannot parse column definition {item!r}')
                    columns.append((_name(col[1]), col[2]))
                    if col[4]:
                        self.defaults[(name.upper(), _name(col[1]))] = col[4].strip()
                    if col[3]:
                        primary_key = _name(col[1])
            if kind == 'NODE':
                if primary_key is None:
                    raise SchemaError(f'NODE table {name} needs a primary key')
                return self.create_node_table(name, columns, primary_key, if_not_exists)
            if not pairs:
                raise SchemaError(f'REL table {name} needs FROM ... TO ...')
            return self.create_rel_table(name, pairs, columns, multiplicity, if_not_exists)
        raise SchemaError(f'Unsupported DDL: {s[:120]}')
