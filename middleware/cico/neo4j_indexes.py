"""Compile declarative Neo4j materializations into middleware FILL definitions.

Only explicit index registration uses this translator; ordinary writes never
register an index. Unsupported writes fail closed instead of guessing a source.
"""
import re

from .errors import Unsupported
from .neo4j import tokenize, render
from .statements import split_script, strip_comments

IDENT = r'(?:`(?:[^`]|``)+`|[A-Za-z_][A-Za-z0-9_]*)'


def flatten(statement):
    tokens = tokenize(strip_comments(statement))
    # Execution options and batching do not affect the materialized relation.
    if tokens and tokens[0].upper == 'CYPHER':
        start = next((i for i, t in enumerate(tokens) if t.upper == 'MATCH'), None)
        if start is None:
            raise Unsupported('index statement has no MATCH')
        tokens = tokens[start:]
    calls = [i for i, t in enumerate(tokens) if t.upper == 'CALL']
    if calls:
        if len(calls) != 1:
            raise Unsupported('nested/multiple CALL index subqueries')
        pos = calls[0]
        opening = next((i for i in range(pos + 1, len(tokens)) if tokens[i].text == '{'), None)
        if opening is None:
            raise Unsupported('index procedure calls')
        depth = 1
        end = opening + 1
        while end < len(tokens) and depth:
            depth += tokens[end].text == '{'
            depth -= tokens[end].text == '}'
            end += 1
        if depth:
            raise Unsupported('unclosed index CALL')
        suffix = render(tokens[end:])
        if not re.fullmatch(r'IN\s+(?:\d+\s+CONCURRENT\s+)?TRANSACTIONS(?:\s+OF\s+\d+\s+ROWS)?', suffix, re.I):
            raise Unsupported('only batched index CALL is supported')
        body = tokens[opening + 1:end - 1]
        # Legacy importing WITH is redundant after flattening.
        if body and body[0].upper == 'WITH':
            stop = next((i for i in range(1, len(body)) if body[i].upper in
                         ('MATCH', 'OPTIONAL', 'WITH', 'SET', 'REMOVE', 'DELETE', 'CREATE', 'MERGE')), None)
            if stop is None:
                raise Unsupported('index CALL body has no operation')
            body = body[stop:]
        tokens = tokens[:pos] + body
    return render(tokens)


def definitions(script, frontend, schema, parameters=None, defaults=None):
    """Return FILL branches grouped by carrier (including all subtype branches).

    Cleanup statements are retained by the caller as build SQL. They do not
    constitute declarations on their own. Conditional/partial writes are rejected.
    """
    groups = {}
    defaults = defaults if defaults is not None else {}
    def convert(query):
        from .middleware import inline_parameters
        from .normalize import fold_constants, normalize_temporal_comparisons
        return normalize_temporal_comparisons(fold_constants(inline_parameters(frontend.to_internal(query), parameters or {})))
    statements = split_script(script)
    for statement in statements:
        text = flatten(statement)
        tokens = tokenize(text)
        depth = 0
        writes = []
        for i, token in enumerate(tokens):
            if depth == 0 and token.upper in ('SET', 'REMOVE', 'DELETE', 'CREATE', 'MERGE'):
                writes.append((i, token.upper))
            depth += token.text in ('(', '[', '{')
            depth -= token.text in (')', ']', '}')
        if not writes:
            raise Unsupported('index statement has no materialization write')
        if len(writes) == 1 and writes[0][1] in ('REMOVE', 'DELETE'):
            continue
        # Fail before registering anything when a statement has mixed writes.
        if len(writes) != 1:
            raise Unsupported('mixed index writes; use one declaration per statement')
        pos, operation = writes[0]
        source = render(tokens[:pos])
        tail = render(tokens[pos + 1:])
        if operation == 'SET':
            reset = re.fullmatch(rf'MATCH\s*\(\s*({IDENT})\s*:\s*({IDENT})\s*\)', source, re.I)
            if reset:
                assignments = re.findall(rf'({IDENT})\s*\.\s*({IDENT})\s*=\s*(0(?:\.0)?)\s*(?:,|$)', tail)
                if assignments and all(v == reset[1] for v, _, _ in assignments) and len(assignments) == tail.count(',') + 1:
                    for _, column, value in assignments:
                        defaults[(frontend.table_to_internal(reset[2].strip('`')).upper(), column.strip('`'))] = value
                    continue
            assignment = re.fullmatch(rf'({IDENT})\s*\.\s*({IDENT})\s*=\s*(.+)', tail, re.S)
            if not assignment:
                raise Unsupported('index SET requires one property assignment')
            var, column, value = assignment.groups()
            if re.search(r'\b(CASE|THEN|ELSE)\b', value, re.I) or any(t.text == ',' for t in tokenize(value)):
                raise Unsupported('conditional or multiple index assignments')
            labels = re.findall(rf'\(\s*{re.escape(var)}\s*:\s*({IDENT})\s*\)', source)
            if not labels:
                raise Unsupported('index carrier must have an explicit node label')
            label = labels[0].strip('`')
            table = frontend.table_to_internal(label)
            # COUNT { MATCH ... } is a total count including zero. Represent it
            # with OPTIONAL MATCH so the generated build has the same default.
            counted = re.search(r'WITH\s+' + re.escape(var) + r'\s*,\s*count\s*\{\s*MATCH\s+(.*?)\s*\}\s+AS\s+(' + IDENT + r')\s*$', source, re.I | re.S)
            if counted:
                pattern, alias = counted.groups()
                # Bind the final counted node; no optional-edge count(*) which
                # would turn the unmatched row into an incorrect count of one.
                nodes = list(re.finditer(r'\(\s*(?:' + IDENT + r')?\s*(?::[^()]*)?\)', pattern))
                if not nodes:
                    raise Unsupported('COUNT index pattern has no counted node')
                node = nodes[-1]
                if not re.match(r'\(\s*:', node.group()):
                    raise Unsupported('COUNT index requires an anonymous final node')
                pattern = pattern[:node.start()] + node.group().replace('(', '(__cico_count', 1) + pattern[node.end():]
                source = source[:counted.start()] + f' OPTIONAL MATCH {pattern} WITH {var}, count(__cico_count) AS {alias}'
            aggregate = re.search(r'\bWITH\s+' + re.escape(var) + r'\s*,\s*(count\([^{}]*\))\s+AS\s+' + re.escape(value.strip()) + r'\s*$', source, re.I)
            if aggregate:
                value = aggregate[1]
                source = source[:aggregate.start()]
            query = f'{source} RETURN {var}.id, {value}'

            internal = convert(query)
            key = (table, column.strip('`'))
            fill = f'FILL {table}({column}) FROM ({internal})'
        else:
            edge = re.fullmatch(rf'\(\s*({IDENT})\s*\)\s*-\s*\[\s*:\s*({IDENT})\s*\]\s*->\s*\(\s*({IDENT})\s*\)', tail)
            if not edge:
                raise Unsupported('derived index requires CREATE/MERGE (src)-[:TYPE]->(dst)')
            src, table, dst = edge.groups()
            if operation == 'CREATE' and re.search(r'\bDISTINCT\b', source, re.I) is None and src != dst:
                raise Unsupported('derived CREATE requires DISTINCT endpoint pairs; use MERGE')
            source = re.sub(r'\bWITH\s+DISTINCT\s+' + re.escape(src) + r'\s*,\s*' + re.escape(dst) + r'\s*$', '', source, flags=re.I)
            endpoints = f'{src}.id, {dst}.id' if src != dst else f'{src}.id AS __cico_src, {dst}.id AS __cico_dst'
            internal = convert(f'{source} RETURN DISTINCT {endpoints}')
            key = (table, '')
            fill = f'FILL {table}() FROM ({internal})'
        groups.setdefault(key, []).append(fill)
    if not groups:
        raise Unsupported('no declarative indexes in script')
    # A zero reset followed by sparse grouped counts denotes a total count
    # per carrier. Include unmatched carriers in the declaration as OPTIONAL.
    from .statements import parse_fill
    for key, branches in groups.items():
        if defaults.get((key[0].upper(), key[1])) != '0' or len(branches) != 1:
            continue
        fill = parse_fill(branches[0])
        total = re.fullmatch(r'MATCH\s*(.+?)\s+RETURN\s+(\w+)\.id\s*,\s*(count\(.+\))', fill.source, re.I | re.S)
        if total and 'OPTIONAL' not in total[1].upper() and not re.search(r'\bWITH\b', total[1], re.I):
            carrier = total[2]
            branches[0] = f'FILL {fill.table}({key[1]}) FROM (MATCH ({carrier}:{fill.table}) OPTIONAL MATCH {total[1]} RETURN {carrier}.id, {total[3]})'
    # Fuse the self edge and positive closure of BI9 into one zero-length
    # closure. The engine requires one definition to own a derived edge table.
    # Exact matching keeps unrelated multi-branch declarations untouched.
    for key, branches in groups.items():
        if len(branches) != 2 or key[1]:
            continue
        first = re.sub(r'\s+', ' ', branches[0]).strip()
        second = re.sub(r'\s+', ' ', branches[1]).strip()
        a = re.fullmatch(r'FILL (\w+)\(\) FROM \(MATCH \((\w+):(\w+)\) RETURN DISTINCT \2.id AS __cico_src, \2.id AS __cico_dst\)', first)
        b = re.fullmatch(r'FILL (\w+)\(\) FROM \(MATCH \((\w+):(\w+)\) MATCH \(\2\)-\[:(\w+)\*1\.\.4294967295\]->\((\w+):(\w+)\) RETURN DISTINCT \2.id, \5.id\)', second)
        if a and b and a[1] == b[1] and a[3] == b[6]:
            acyclic = {r for name in frontend.acyclic for r in frontend.rels.get(name, [name])}
            if b[4] not in acyclic:
                raise Unsupported('closure index requires an acyclic relationship type')
            groups[key] = [f'FILL {a[1]}() FROM (MATCH (__cico_post:{a[3]})<-[:{b[4]}*0..4294967295]-(__cico_message:{b[3]}:{a[3]}) RETURN DISTINCT __cico_message.id, __cico_post.id)']
    return list(groups.values())
