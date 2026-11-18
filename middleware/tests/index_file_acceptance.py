"""Neo4j small-graph checks for durable FILL reload; never executes BI queries."""
import argparse
import collections
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware, IndexFileCatalog
from cico.index_files import Neo4jIndexExecutor
from cico.neo4j import neo4j_profile
from cico.schema import Schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--uri', default='bolt://127.0.0.1:7693')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    prefix = 'CicoReload' + uuid.uuid4().hex[:12]
    person, city, rel = prefix + 'Person', prefix + 'City', prefix + 'Lives'
    def prop(name):
        return {'property_name': name, 'property_type': {'primitive_type': 'DT_SIGNED_INT64'}}
    schema = Schema({'schema': {'vertex_types': [
        {'type_name': person, 'type_id': 0, 'properties': [prop('id')], 'primary_keys': ['id']},
        {'type_name': city, 'type_id': 1, 'properties': [prop('id'), prop('score')], 'primary_keys': ['id']},
    ], 'edge_types': [{'type_name': rel, 'type_id': 0, 'properties': [], 'vertex_type_pair_relations': [
        {'source_vertex': person, 'destination_vertex': city, 'relation': 'MANY_TO_MANY'}]}]}})
    source = f'MATCH (p:{person})-[:{rel}]->(c:{city}) RETURN p.id, count(c)'
    first = f'ALTER TABLE {person} ADD IF NOT EXISTS cached INT64 DEFAULT 0; FILL {person}(cached) FROM ({source});'
    second = f'ALTER TABLE {person} ADD IF NOT EXISTS doubled INT64 DEFAULT 0; FILL {person}(doubled) FROM (MATCH (p:{person}) RETURN p.id,p.cached * 2);'
    query = source.replace('RETURN p.id, count(c)', 'RETURN p.id AS personId, count(c) AS cnt ORDER BY personId')
    report = {'workload_queries_executed': False, 'small_graph_queries_executed': True, 'checks': [],
              'target': None, 'cleanup': False}
    runner = Neo4jIndexExecutor(args.uri)
    report['target'] = json.loads(runner.target)
    try:
        with runner.driver.session(database=runner.database) as session:
            session.run(f'CREATE (p1:{person} {{id:1}}),(p2:{person} {{id:2}}),(p3:{person} {{id:3}}),'
                        f'(c1:{city} {{id:1,score:9}}),(c2:{city} {{id:2}}),'
                        f'(p1)-[:{rel}]->(c1),(p1)-[:{rel}]->(c2),(p2)-[:{rel}]->(c2)').consume()
        with tempfile.TemporaryDirectory(prefix='cico-reload-') as tmp:
            path = Path(tmp) / 'indexes.cypher'
            state = Path(tmp) / 'build-state.json'
            path.write_text(first)
            # Identity mapping for these private labels.
            mapping = Path(tmp) / 'mapping.yaml'
            mapping.write_text('labels: {}\ntyped_labels: {}\nrel_types: {}\n')
            executed = []
            def execute(statement):
                with runner.driver.session(database=runner.database) as session:
                    session.run('EXPLAIN ' + statement).consume()
                runner(statement)
                executed.append(statement)
            def compare(mw, text):
                rewritten = mw.execute(text)
                assert rewritten.rewritten, rewritten.to_dict()
                with runner.driver.session(database=runner.database) as session:
                    session.run('EXPLAIN ' + rewritten.cypher).consume()
                    before = [tuple(row.values()) for row in session.run(text)]
                    after = [tuple(row.values()) for row in session.run(rewritten.cypher)]
                assert collections.Counter(before) == collections.Counter(after), (before, after, rewritten.cypher)
                return {'query': text, 'output': rewritten.cypher, 'rows': before, 'equal': True}
            with IndexMiddleware(schema.copy(), profile=neo4j_profile(mapping)) as mw:
                catalog = IndexFileCatalog(mw, [path], state, runner.target)
                cold = catalog.reload(execute)
                assert len(cold['built']) == 1
                checked = compare(mw, query)
                assert any('__neug_fill_state_n_rows_' in s for s in executed)
                hidden = [c for entry in mw.indexes()['indexes'] for c in entry['target_columns']
                          if c.startswith('__neug_fill_state_n_rows_')]
                assert len(hidden) == 1
                with runner.driver.session(database=runner.database) as session:
                    physical = [tuple(row.values()) for row in session.run(
                        f'MATCH (p:{person}) RETURN p.id,p.cached,p.`{hidden[0]}` ORDER BY p.id')]
                assert physical == [(1, 2, 2), (2, 1, 1), (3, 0, 0)], physical
                report['checks'].append({'name': 'initial_build_count_and_hidden_state',
                                         'writes': len(executed), 'physical_count_and_state': physical, **checked})
            executed.clear()
            with IndexMiddleware(schema.copy(), profile=neo4j_profile(mapping)) as mw:
                catalog = IndexFileCatalog(mw, [path], state, runner.target)
                restart = catalog.reload(execute)
                assert not executed and not restart['pending']
                report['checks'].append({'name': 'restart_no_rebuild', 'writes': 0, **compare(mw, query)})
                path.write_text('// formatting edit\n' + first.replace('MATCH ', 'match\n ').replace('RETURN ', 'return\n '))
                formatting = catalog.reload(execute)
                assert not executed and formatting['unchanged'] == restart['unchanged']
                report['checks'].append({'name': 'formatting_no_rebuild', 'writes': 0, **compare(mw, query)})
                path.write_text(first + second)
                added = catalog.reload(execute)
                assert added['built'] == [person.upper() + '(doubled)']
                assert not any('SET p.cached =' in s or 'SET cico_n.cached =' in s for s in executed)
                report['checks'].append({'name': 'add_only_builds_new', 'built': added['built'],
                                         'writes': len(executed), **compare(mw, query)})
                executed.clear()
                path.write_text(first.replace('RETURN p.id', 'WHERE c.id >= 2 RETURN p.id') + second)
                changed = catalog.reload(execute)
                assert changed['built'] == [person.upper() + '(cached)', person.upper() + '(doubled)']
                filtered = query.replace('RETURN p.id', 'WHERE c.id >= 2 RETURN p.id')
                report['checks'].append({'name': 'change_rebuilds_dependency_chain', 'built': changed['built'],
                                         'writes': len(executed), **compare(mw, filtered)})
                executed.clear()
                cached_rel = prefix + 'CachedLives'
                path.write_text(path.read_text() + f'\nFILL {cached_rel}() FROM (MATCH (p:{person})-[:{rel}]->(c:{city}) RETURN DISTINCT p.id,c.id);')
                derived = catalog.reload(execute)
                assert derived['built'] == [cached_rel.upper() + '()']
                derived_query = f'MATCH (p:{person})-[:{rel}]->(c:{city}) RETURN DISTINCT p.id AS personId,c.id AS cityId'
                report['checks'].append({'name': 'native_derived_relationship_materializes',
                                         'built': derived['built'], 'writes': len(executed), **compare(mw, derived_query)})
                report['state'] = json.loads(state.read_text())
    finally:
        try:
            with runner.driver.session(database=runner.database) as session:
                session.run(f'MATCH (n) WHERE n:{person} OR n:{city} DETACH DELETE n').consume()
                remaining = session.run(f'MATCH (n) WHERE n:{person} OR n:{city} RETURN count(n) AS c').single()['c']
                report['cleanup'] = remaining == 0
        finally:
            runner.close()
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    assert report['cleanup']
    print(json.dumps({'checks': len(report['checks']), 'cleanup': report['cleanup'], 'output': args.output}))


if __name__ == '__main__':
    main()
