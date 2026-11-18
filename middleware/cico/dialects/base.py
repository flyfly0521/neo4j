"""Dialect hooks used by the Bound IR printer.

A dialect decides how neutral constructs (labels, functions, literals,
identifiers, variable-length bounds) are spelled for one target system, and
may map schema names (see neo4j.py).  The base dialect is NeuG/Kuzu Cypher.
"""
import re

from ..errors import Unsupported

_SIMPLE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

KEYWORDS = {
    'ADD', 'ALL', 'ALTER', 'AND', 'AS', 'ASC', 'ASCENDING', 'BY', 'CALL', 'CASE', 'CAST', 'COPY',
    'COUNT', 'CREATE', 'DELETE', 'DESC', 'DESCENDING', 'DETACH', 'DISTINCT', 'DROP', 'ELSE', 'END',
    'ENDS', 'EXISTS', 'FALSE', 'FILL', 'FROM', 'IN', 'IS', 'LIMIT', 'LOAD', 'MATCH', 'MERGE', 'NOT',
    'NULL', 'ON', 'OPTIONAL', 'OR', 'ORDER', 'PROFILE', 'RETURN', 'SET', 'SKIP', 'STARTS', 'THEN',
    'TO', 'TRUE', 'UNION', 'UNWIND', 'USE', 'WHEN', 'WHERE', 'WITH', 'XOR', 'CONTAINS', 'SHORTEST',
    'TABLE', 'EXPLAIN', 'GROUP', 'REL', 'NODE', 'PRIMARY', 'KEY', 'MACRO', 'YIELD', 'STAR', 'LIMIT',
}

INTEGER = {'INT8', 'INT16', 'INT32', 'INT64', 'INT128', 'UINT8', 'UINT16', 'UINT32', 'UINT64', 'SERIAL'}
FLOAT = {'FLOAT', 'DOUBLE'}

INFIX = {
    'AND': 'AND', 'OR': 'OR', 'XOR': 'XOR',
    'EQUALS': '=', 'NOT_EQUALS': '<>', 'GREATER_THAN': '>', 'GREATER_THAN_EQUALS': '>=',
    'LESS_THAN': '<', 'LESS_THAN_EQUALS': '<=',
    '+': '+', '-': '-', '*': '*', '/': '/', '%': '%', '^': '^',
    'STARTS_WITH': 'STARTS WITH', 'ENDS_WITH': 'ENDS WITH', 'CONTAINS': 'CONTAINS',
    'REGEXP_FULL_MATCH': '=~',
}
POSTFIX = {'IS_NULL': 'IS NULL', 'IS_NOT_NULL': 'IS NOT NULL'}
PREFIX = {'NOT': 'NOT ', 'NEGATE': '-'}
AGGREGATES = {'COUNT_STAR', 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COLLECT'}
UNBOUNDED = 4294967295


def struct_fields(type_text):
    """Field names of a type string such as STRUCT(score INT64, id INT64)."""
    m = re.fullmatch(r'STRUCT\((.*)\)', type_text or '')
    if not m:
        return None
    fields, depth, current = [], 0, ''
    for ch in m[1]:
        depth += ch == '('
        depth -= ch == ')'
        if ch == ',' and depth == 0:
            fields.append(current.strip().split(' ')[0])
            current = ''
        else:
            current += ch
    if current.strip():
        fields.append(current.strip().split(' ')[0])
    return fields


class Dialect:
    name = 'neug'
    list_index_base = 0  # added to a printed list index

    def __init__(self):
        # NeuG binds a multi-pair relationship group as child tables named
        # <GROUP>_<SRC>_<DST>; Cypher text must name the group.
        self.rel_group = {}

    def set_schema(self, schema):
        self.rel_group = {}
        for edge in schema.edges:
            for pair in edge.get('vertex_type_pair_relations') or []:
                child = f"{edge['type_name']}_{pair['source_vertex']}_{pair['destination_vertex']}"
                self.rel_group[child] = edge['type_name']

    # ------------------------------------------------- materialization DDL
    def add_column_ddl(self, table, column, type_text, default):
        """Statements creating one index carrier column (Kuzu/NeuG syntax)."""
        tail = f' DEFAULT {default}' if default is not None else ''
        return [f'ALTER TABLE {self.ident(table)} ADD IF NOT EXISTS {self.ident(column)} {type_text}{tail}']

    # Can CREATE take an endpoint bound to a label union (n:A:B)?  Kuzu: no.
    create_on_label_union = False

    def fill_statement(self, body, tail, variables, eager=False, partition=None):
        """`body` ends in WITH <variables>; `tail` writes the carrier."""
        return f'{body} {tail}'

    def create_rel_table_ddl(self, item, schema):
        from ..schema import type_name
        parts = [f"FROM {self.ident(p['source_vertex'])} TO {self.ident(p['destination_vertex'])}"
                 for p in item.get('vertex_type_pair_relations') or []]
        for p in item.get('properties') or []:
            default = schema.defaults.get((item['type_name'].upper(), p['property_name']))
            parts.append(f"{self.ident(p['property_name'])} {type_name(p['property_type'])}"
                         + (f' DEFAULT {default}' if default is not None else ''))
        return [f"CREATE REL TABLE IF NOT EXISTS {self.ident(item['type_name'])}({', '.join(parts)})"]

    # --------------------------------------------------------------- names
    def ident(self, name):
        if not name:
            raise Unsupported('empty identifier')
        if _SIMPLE.match(name) and name.upper() not in KEYWORDS:
            return name
        return '`' + name.replace('`', '``') + '`'

    def node_pattern(self, variable, tables):
        """`tables` is the union of node tables the variable is bound to."""
        return self.ident(variable) + ''.join(':' + self.ident(t) for t in tables)

    def rel_types(self, tables):
        groups = list(dict.fromkeys(self.rel_group.get(t, t) for t in tables))
        return (':' + '|'.join(self.ident(t) for t in groups)) if groups else ''

    def var_length(self, lower, upper):
        return f'*{lower}..{upper}'

    def property(self, owner, prop, uid=None):
        return f'{self.ident(owner)}.{self.ident(prop)}'

    def pending_predicates(self):
        """Extra WHERE conditions created by node_pattern (schema mapping)."""
        return []

    def finish(self, text):
        return text

    # ------------------------------------------------------------ literals
    def string(self, value):
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"

    def literal(self, v):
        type_name = v.get('type', '')
        if v.get('null'):
            if type_name in ('ANY', 'NULL', ''):
                return 'NULL'
            return f'CAST(NULL AS {type_name})'
        if 'items' in v:
            items = [self.literal(i) for i in v['items']]
            if type_name.startswith('STRUCT'):
                return '{' + ', '.join(f'{self.ident(f)}: {i}' for f, i in zip(v['fields'], items)) + '}'
            return '[' + ', '.join(items) + ']'
        text = v.get('text', '')
        if type_name in INTEGER:
            if not re.fullmatch(r'-?\d+', text):
                raise Unsupported(f'integer literal {text!r}')
            return f'({text})' if text.startswith('-') else text
        if type_name in FLOAT:
            if not re.fullmatch(r'-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?', text):
                raise Unsupported(f'float literal {text!r}')
            if not re.search(r'[.eE]', text):
                text += '.0'
            return f'({text})' if text.startswith('-') else text
        if type_name == 'STRING':
            return self.string(text)
        if type_name == 'BOOL':
            if text.lower() not in ('true', 'false'):
                raise Unsupported(f'bool literal {text!r}')
            return text.lower()
        return self.temporal_literal(type_name, text)

    def temporal_literal(self, type_name, text):
        if type_name == 'DATE':
            return f'date({self.string(text)})'
        if type_name.startswith('TIMESTAMP'):
            return f'timestamp({self.string(text)})'
        if type_name == 'INTERVAL':
            return f'interval({self.string(text)})'
        raise Unsupported(f'literal type {type_name}')

    def parameter(self, name):
        return '$' + self.ident(name)

    # ----------------------------------------------------------- functions
    def aggregate(self, name, args, distinct):
        if name == 'COUNT_STAR':
            return 'count(*)'
        if name not in AGGREGATES:
            raise Unsupported(f'aggregate {name}')
        return f'{name.lower()}(' + ('DISTINCT ' if distinct else '') + ', '.join(args) + ')'

    def match_text(self, keyword, parts, predicate):
        """`MATCH p1, p2, ...`.  Kuzu/NeuG match relationships as a
        homomorphism, so one clause is exact."""
        return f'{keyword} ' + ', '.join(text for text, _ in parts)

    def fold_node_labels(self, graphs, predicate, declared):
        """({node uid: label text}, remaining predicate): conjuncts of a MATCH
        predicate the dialect prints as the label of a node the clause
        declares.  Default: none."""
        return {}, predicate

    def special_expression(self, e, printer):
        """Hook for dialects that spell a whole expression differently
        (returns text) or None to print it generically."""
        return None

    def label_of(self, arg):
        return f'label({arg})'

    def cast(self, arg, type_name):
        return f'CAST({arg} AS {type_name})'

    def function(self, name, args, e=None):
        if name in INFIX:
            if len(args) == 2:
                return f'({args[0]} {INFIX[name]} {args[1]})'
            if name in ('AND', 'OR', 'XOR') and len(args) > 2:
                return '(' + f' {INFIX[name]} '.join(args) + ')'
            raise Unsupported(f'{name} with {len(args)} arguments')
        if name in POSTFIX:
            return f'({args[0]} {POSTFIX[name]})'
        if name in PREFIX:
            return f'({PREFIX[name]}{args[0]})'
        if name == 'LIST_CREATION':
            return '[' + ', '.join(args) + ']'
        if name in ('LIST_CONTAINS', 'LIST_HAS'):
            return f'({args[1]} IN {args[0]})'
        if name in ('LIST_EXTRACT', 'LIST_ELEMENT'):
            return f'{args[0]}[{args[1]}]'
        if name == 'STRUCT_EXTRACT':
            child = (e or {}).get('children', [None, None])[1] or {}
            field = (child.get('value') or {}).get('text')
            if child.get('k') != 'LITERAL' or not field:
                raise Unsupported('dynamic struct field')
            return f'{args[0]}.{self.ident(field)}'
        if name == 'STRUCT_PACK':
            fields = struct_fields((e or {}).get('type'))
            if not fields or len(fields) != len(args):
                raise Unsupported('struct literal fields')
            return '{' + ', '.join(f'{self.ident(f)}: {a}' for f, a in zip(fields, args)) + '}'
        if name.startswith('CAST'):
            return self.cast(args[0], (e or {}).get('type'))
        if not _SIMPLE.match(name):
            raise Unsupported(f'function {name!r}')
        return self.call(name, args)

    # Binder-internal names whose surface spelling differs.
    SPELLING = {'TO_INTERVAL': 'duration'}

    def call(self, name, args):
        return f"{self.SPELLING.get(name, name)}(" + ', '.join(args) + ')'
