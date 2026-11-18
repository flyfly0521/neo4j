"""Build the middleware schema (NeuG schema YAML) from a target system.

The middleware must bind queries with the *target's* property types (e.g. a
Kuzu LDBC store keeps creationDate as TIMESTAMP while a NeuG store may keep
INT64).  `kuzu_schema` reads them from a Kuzu database; relationship
multiplicities, which Kuzu does not report through its catalog functions, can
be borrowed from a reference NeuG schema with the same table names.

  python -m cico schema --kuzu DB [--reference graph.yaml] > schema.yaml
"""
import sys

import yaml

from .schema import Schema, SchemaError, property_type


def _rows(conn, query):
    result = conn.execute(query)
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


def _prop(i, name, type_text, skipped, table):
    try:
        return dict(property_id=i, property_name=name, property_type=property_type(type_text))
    except SchemaError:
        skipped.append(f'{table}.{name}: {type_text}')
        return None


def kuzu_schema(db_path, reference=None, name=None):
    import kuzu
    conn = kuzu.Connection(kuzu.Database(str(db_path), read_only=True))
    ref = Schema.from_file(reference) if reference else None
    multiplicity = {}
    if ref is not None:
        for edge in ref.edges:
            for pair in edge.get('vertex_type_pair_relations') or []:
                key = (edge['type_name'].upper(), pair['source_vertex'].upper(), pair['destination_vertex'].upper())
                multiplicity[key] = pair.get('relation', 'MANY_TO_MANY')
    tables = sorted(_rows(conn, 'CALL show_tables() RETURN *'), key=lambda r: (r[2], r[1]))
    vertices, edges, skipped = [], [], []
    for _, table, kind, *_ in tables:
        info = _rows(conn, f"CALL table_info('{table}') RETURN *")
        if kind == 'NODE':
            props = [p for p in (_prop(len_i, r[1], r[2], skipped, table)
                                 for len_i, r in enumerate(info)) if p]
            primary = [r[1] for r in info if r[4] is True]
            vertices.append(dict(type_name=table, type_id=len(vertices), properties=props,
                                 primary_keys=primary))
        elif kind == 'REL':
            props = [p for p in (_prop(i, r[1], r[2], skipped, table) for i, r in enumerate(info)) if p]
            pairs = [dict(source_vertex=src, destination_vertex=dst,
                          relation=multiplicity.get((table.upper(), src.upper(), dst.upper()), 'MANY_TO_MANY'))
                     for src, dst, *_ in _rows(conn, f"CALL show_connection('{table}') RETURN *")]
            edges.append(dict(type_name=table, type_id=len(edges), properties=props,
                              vertex_type_pair_relations=pairs))
    doc = dict(name=name or str(db_path).rstrip('/').split('/')[-1], store_type='mutable_csr',
               schema=dict(vertex_types=vertices, edge_types=edges))
    return Schema(doc), skipped


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='cico schema')
    parser.add_argument('--kuzu', required=True, help='Kuzu database directory (opened read-only)')
    parser.add_argument('--reference', help='NeuG graph.yaml to borrow relationship multiplicities from')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    schema, skipped = kuzu_schema(args.kuzu, args.reference)
    for item in skipped:
        print(f'warning: property type not representable, omitted: {item}', file=sys.stderr)
    text = schema.to_yaml()
    if args.output:
        with open(args.output, 'w') as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
