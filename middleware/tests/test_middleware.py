"""Unit + integration tests.  Integration tests need engine/build/incgq_rewrite_engine.

  python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware, Unsupported, print_statement  # noqa: E402
from cico.canonical import canonical, first_difference, raw_names  # noqa: E402
from cico.dialects import get_dialect  # noqa: E402
from cico.engine import DEFAULT_ENGINE  # noqa: E402
from cico.neo4j import neo4j_profile  # noqa: E402
from cico.schema import Schema  # noqa: E402
from cico.statements import classify, parse_fill, split_script  # noqa: E402


def schema_yaml():
    def prop(i, name, kind='DT_SIGNED_INT64'):
        return f"""        - property_id: {i}
          property_name: {name}
          property_type:
            primitive_type: {kind}
"""
    vertices = ''
    for tid, label in enumerate(('Person', 'City', 'Restaurant')):
        vertices += f"""    - type_name: {label}
      type_id: {tid}
      properties:
{prop(0, 'id')}      primary_keys: [id]
"""
    edges = ''
    for tid, (name, src, dst) in enumerate((('LIVES_IN', 'Person', 'City'), ('HAS_RESTAURANT', 'City', 'Restaurant'))):
        edges += f"""    - type_name: {name}
      type_id: {tid}
      properties: []
      vertex_type_pair_relations:
        - source_vertex: {src}
          destination_vertex: {dst}
          relation: MANY_TO_MANY
"""
    return f'schema:\n  vertex_types:\n{vertices}  edge_types:\n{edges}'


FILL = ('FILL Person(iv_count) FROM (MATCH (a:Person)-[:LIVES_IN]->(b:City)-[:HAS_RESTAURANT]->'
        '(c:Restaurant) RETURN a.id, count(*))')
PATH = 'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) '
PROFILE_DIR = Path(__file__).resolve().parents[1] / 'profiles'


class UnitTests(unittest.TestCase):
    def test_identifiers_and_literals(self):
        d = get_dialect('neug')
        self.assertEqual(d.ident('person'), 'person')
        self.assertEqual(d.ident('count'), '`count`')
        self.assertEqual(d.ident('iv_x-native_v0'), '`iv_x-native_v0`')
        self.assertEqual(d.literal(dict(null=False, text="it's", type='STRING')), "'it\\'s'")
        self.assertEqual(d.literal(dict(null=False, text='-3', type='INT64')), '(-3)')
        self.assertEqual(d.literal(dict(null=False, text='2', type='DOUBLE')), '2.0')
        self.assertEqual(d.literal(dict(null=False, type='INT64[]', items=[
            dict(null=False, text='1', type='INT64'), dict(null=False, text='2', type='INT64')])), '[1, 2]')

    def test_printer_rejects_unsupported_shapes(self):
        with self.assertRaises(Unsupported):
            print_statement(dict(statement_type=0, single_queries=[], unsupported=True))

    def test_script_split_and_classify(self):
        script = "// c\nALTER TABLE P ADD x INT64; FILL P(x) FROM (MATCH (a) RETURN a.id, ';');\nCHECKPOINT;\nMATCH (n) RETURN n"
        parts = split_script(script)
        self.assertEqual([classify(p) for p in parts], ['ddl', 'fill', 'noop', 'query'])
        fill = parse_fill(parts[1])
        self.assertEqual((fill.table, fill.columns), ('P', ['x']))
        self.assertIn("';'", fill.source)

    def test_schema_ddl(self):
        s = Schema.from_text(schema_yaml())
        self.assertTrue(s.apply_ddl('ALTER TABLE Person ADD IF NOT EXISTS score DOUBLE DEFAULT 1.0'))
        self.assertFalse(s.apply_ddl('ALTER TABLE Person ADD IF NOT EXISTS score DOUBLE'))
        self.assertEqual(s.properties('Person')['score'], 'DOUBLE')
        self.assertTrue(s.apply_ddl('CREATE REL TABLE IF NOT EXISTS NEAR(FROM Person TO City, FROM City TO City, w INT64)'))
        self.assertEqual(len(s.table('NEAR')[1]['vertex_type_pair_relations']), 2)
        self.assertTrue(s.apply_ddl('DROP TABLE IF EXISTS NEAR'))

    def test_first_difference(self):
        self.assertIsNone(first_difference(dict(a=[1, 2]), dict(a=[1, 2])))
        self.assertIn('$.a[1]', first_difference(dict(a=[1, 2]), dict(a=[1, 3])))


class Neo4jTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = neo4j_profile()
        cls.fe = cls.profile.frontend

    def test_labels_types_and_ranges(self):
        out = self.fe.to_internal('MATCH (c:Country {name: $c})<-[:IS_PART_OF]-(:City)<-[:IS_LOCATED_IN]-'
                                  '(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:REPLY_OF*0..]->(post:Post) RETURN p.id')
        self.assertIn("(c:PLACE {name: $c, type: 'Country'})", out)
        self.assertIn("(:PLACE {type: 'City'})", out)
        self.assertIn('[:ISLOCATEDIN|ORGANISATION_ISLOCATEDIN]', out)
        self.assertIn('(m:COMMENT:POST)', out)
        self.assertIn('[:REPLYOF*0..4294967295]', out)

    def test_expressions(self):
        out = self.fe.to_internal('MATCH (a:Forum), (b:Person) WHERE NOT (a)-[:HAS_MEMBER]->(b) AND a:Forum '
                                  'RETURN toFloat(count(*)) AS f, $d.year AS y, b.creationDate.month AS m')
        self.assertIn('NOT EXISTS { MATCH (a)-[:HASMEMBER]->(b) }', out)
        self.assertIn("(label(a) = 'FORUM')", out)
        self.assertIn('CAST(count(*) AS DOUBLE)', out)
        self.assertIn("date_part('year', $d)", out)
        self.assertIn("date_part('month', b.creationDate)", out)

    def test_unsupported_constructs(self):
        for query in ('MATCH (a) CALL (a) { RETURN 1 AS x } RETURN x',
                      'MATCH (a:Message) WHERE a.creationDate > $d + duration({hours: 4, minutes: 2}) RETURN a'):
            with self.assertRaises(Unsupported):
                self.fe.to_internal(query)

    def test_normalizations(self):
        out = self.fe.to_internal('MATCH (m:Message) WHERE $a < m.creationDate <= $b '
                                  'RETURN coalesce(m.length, 0) AS l, m.creationDate + duration({hours: $h}) AS t')
        self.assertIn('$a < m.creationDate AND m.creationDate <= $b', out)
        self.assertIn('CASE WHEN (m.length) IS NOT NULL THEN (m.length) ELSE (0) END', out)
        self.assertIn("duration(CAST($h AS STRING) + ' hours')", out)
        self.assertIn("duration('4 hours')", self.fe.to_internal("RETURN duration('PT4H')"))
        self.assertEqual(self.profile.dialect.call('TO_INTERVAL', ["'1 day 4 hours'"]), "duration('P1DT4H')")

    def test_relationship_uniqueness(self):
        # Neo4j binds the relationships of one MATCH to distinct edges.
        out = self.fe.to_internal('MATCH (a:Person)-[:KNOWS]-(b:Person)-[:KNOWS]-(c:Person) RETURN count(*)')
        self.assertIn('WHERE cico_u1 <> cico_u2', out)  # same tables: plain comparison
        # different table sets: identities are compared (NeuG asserts on `r1 <> r2`)
        from cico.neo4j import render, tokenize
        from cico.normalize import entity_id_comparisons
        out = render(entity_id_comparisons(tokenize('MATCH (a)-[r1:A]->(b), (c)-[r2:A|B]->(d) WHERE r1 <> r2 RETURN a')))
        self.assertIn('id(r1) <> id(r2)', out)
        # a variable-length relationship sharing a type with another one of the clause
        with self.assertRaises(Unsupported):
            self.fe.to_internal('MATCH (a:Message)-[:REPLY_OF]->(b:Message)-[:REPLY_OF*]->(p:Post) RETURN p')
        # implied by an endpoint inequality / separate clauses: nothing added
        out = self.fe.to_internal('MATCH (t1:Tag)<-[:HAS_TAG]-(m:Comment)-[:HAS_TAG]->(t2:Tag) WHERE t1 <> t2 RETURN m')
        self.assertNotIn('cico_u', out)
        out = self.fe.to_internal('MATCH (a:Person)-[:KNOWS]-(b:Person) MATCH (b)-[:KNOWS]-(c:Person) RETURN c')
        self.assertNotIn('cico_u', out)

    def test_neo4j_only_constructs(self):
        fe = self.fe
        # plan hints are dropped
        out = fe.to_internal('MATCH (p:Person {id: $id}) USING INDEX p:Person(id) WHERE p.id > 0 RETURN p')
        self.assertNotIn('USING', out)
        self.assertIn('WHERE p.id > 0', out)
        # round(x) == round(x, 0) on Neo4j
        self.assertIn('round(p.x * 2, 0)', fe.to_internal('MATCH (p:Person) RETURN round(p.x * 2) AS r'))
        # shortestPath -> SHORTEST; lower bound 0 and path predicates are rejected
        out = fe.to_internal('MATCH (a:Person {id: 1}), (b:Person {id: 2}), p = shortestPath((a)-[:KNOWS*]-(b)) '
                             'RETURN length(p)')
        self.assertIn('p = (a)-[:KNOWS* SHORTEST 1..4294967295]-(b)', out)
        out = fe.to_internal('MATCH p = allShortestPaths((a:Person)-[:KNOWS*..5]-(b:Person)) RETURN p')
        self.assertIn('* ALL SHORTEST 1..5', out)
        for bad in ('MATCH p = allShortestPaths((a:Person)-[:KNOWS*0..]-(b:Person)) RETURN p',
                    'MATCH p = shortestPath((a:Person)-[r:KNOWS*]-(b:Person)) WHERE all(x IN r WHERE x.y > 0) RETURN p',
                    'MATCH (a:Person) CALL apoc.path.subgraphNodes(a, {}) YIELD node RETURN node',
                    'MATCH (a:Person) RETURN a{.id, .firstName}',
                    'MATCH (a:Person) RETURN datetime({epochMillis: a.birthday})'):
            with self.assertRaises(Unsupported, msg=bad):
                fe.to_internal(bad)
        # variable-length relationships are trails unless the type is acyclic
        self.assertIn('[:KNOWS* TRAIL 1..2]', fe.to_internal('MATCH (a:Person)-[:KNOWS*1..2]-(b:Person) RETURN b'))
        self.assertIn('[:REPLYOF*0..4294967295]', fe.to_internal('MATCH (a:Message)-[:REPLY_OF*0..]->(b:Post) RETURN b'))
        # entity equality between different table sets compares identities
        out = fe.to_internal('MATCH (a:Person)-[:KNOWS*1..2]-(b) WHERE NOT a = b RETURN b')
        self.assertIn('NOT id(a) = id(b)', out)  # no schema here: b stays unlabelled

    def test_surface_check(self):
        from cico.surface import check
        for bad in ('MATCH (a:P) RETURN size([1])', 'MATCH (a:P) RETURN [x IN [1] | x]',
                    'MATCH (a:P) RETURN [1, 2][1..]', "MATCH (a:P) RETURN a.name + 'x'",
                    'MATCH (a:P) RETURN collect({k: a.x})', 'MATCH (a:P) CALL foo() RETURN a'):
            with self.assertRaises(Unsupported, msg=bad):
                check(bad)
        check('MATCH (a:P {id: 1})-[r:R* SHORTEST 1..3]->(b) WHERE EXISTS { MATCH (a)-[:S]->() } RETURN count(*)')

    def test_dialect_mapping(self):
        d = self.profile.dialect
        self.assertEqual(d.node_pattern('m', ['COMMENT', 'POST']), 'm:Message')
        self.assertEqual(d.node_pattern('p', ['PERSON']), 'p:Person')
        self.assertEqual(d.rel_types(['ISLOCATEDIN']), ':IS_LOCATED_IN')
        self.assertEqual(d.var_length(0, 4294967295), '*0..')
        self.assertEqual(d.cast('x', 'DOUBLE'), 'toFloat(x)')
        self.assertEqual(d.literal(dict(null=False, text='2012-12-17 00:00:00', type='TIMESTAMP')),
                         "datetime('2012-12-17T00:00:00')")

    def test_typed_label_folds_into_pattern(self):
        # (c:Place) WHERE c.type = 'City'  ->  (c:City): Neo4j multiplies the
        # selectivities of two labels, which misplanned bi-3.
        d = self.profile.dialect
        d.entity_tables = {'c': ['PLACE'], 'k': ['PLACE']}
        typed = dict(function='EQUALS', children=[dict(k='PROPERTY', owner='c', property='type'),
                                                  dict(k='LITERAL', value=dict(type='STRING', text='City'))])
        other = dict(function='EQUALS', children=[dict(k='PROPERTY', owner='k', property='name'),
                                                  dict(k='PARAMETER', name='country')])
        graphs = [dict(nodes=[dict(uid='c'), dict(uid='k')], rels=[])]
        folds, rest = d.fold_node_labels(graphs, dict(function='AND', children=[typed, other]), set())
        self.assertEqual(folds, {'c': 'City'})
        self.assertIs(rest, other)
        # a node declared by an earlier clause keeps the WHERE label test
        self.assertEqual(d.fold_node_labels(graphs, typed, {'c'}), ({}, typed))


@unittest.skipUnless(DEFAULT_ENGINE.is_file(), 'build engine/ first')
class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cico_test_')
        path = Path(self.tmp.name) / 'schema.yaml'
        path.write_text(schema_yaml())
        self.mw = IndexMiddleware(path)
        self.fill = self.mw.execute(FILL)

    def tearDown(self):
        self.mw.close()
        self.tmp.cleanup()

    def test_fill_infers_missing_carrier(self):
        self.assertTrue(self.fill.ok, self.fill.detail)
        self.assertEqual(self.fill.index_id, 'Person(iv_count)')
        self.assertEqual(self.fill.inferred_ddl, ['ALTER TABLE Person ADD iv_count INT64'])
        self.assertIn('iv_count', self.mw.schema.properties('Person'))

    def test_rewrite_is_verified(self):
        r = self.mw.execute(PATH + 'WHERE x.id > $m RETURN x.id AS person, count(*) AS n ORDER BY n DESC LIMIT 5',
                            parameters=dict(m=3))
        self.assertTrue(r.rewritten, r.detail)
        self.assertEqual(r.indexes, ['Person(iv_count)'])
        self.assertIn('x.iv_count', r.cypher)
        self.assertNotIn('LIVES_IN', r.cypher)
        self.assertIn('$m', r.cypher)

    def test_with_scope_and_unwind(self):
        r = self.mw.execute(PATH + 'WITH x, count(*) AS n WHERE n > 3 RETURN x.id AS id, n')
        self.assertTrue(r.rewritten, r.detail)
        self.assertIn('WHERE (n > 3)', r.cypher)

    def test_each_query_part_is_rewritten_on_its_own(self):
        # Part 2 calls a function the internal binder does not know: the whole
        # statement cannot be compiled, but part 1 is rewritten on a probe
        # (part 1 + RETURN its outputs) and spliced back; part 2 is kept as is.
        query = PATH + 'WITH x, count(*) AS n MATCH (x)-[:LIVES_IN]->(c:City) RETURN c.id, no_such_fn(n) AS v'
        r = self.mw.execute(query)
        self.assertTrue(r.rewritten, r.detail)
        self.assertEqual(r.parts, [1])
        self.assertIn('x.iv_count', r.cypher)
        self.assertTrue(r.cypher.endswith('MATCH (x)-[:LIVES_IN]->(c:City) RETURN c.id, no_such_fn(n) AS v'))
        whole = IndexMiddleware(self.mw.schema, scope='query')
        try:
            self.assertFalse(whole.execute(query).rewritten)
        finally:
            whole.close()
        # Part 2 is rewritten with a stub that binds only part 1's outputs.
        query = 'MATCH (x:Person) WITH x, x.id AS k ' + PATH.replace('MATCH ', 'MATCH ', 1) \
            .replace('(x:Person)', '(x)') + 'RETURN k, count(*) AS n'
        r = self.mw.execute(query)
        self.assertTrue(r.rewritten, r.detail)
        self.assertEqual(r.parts, [2])
        self.assertTrue(r.cypher.startswith('MATCH (x:Person) WITH x, x.id AS k '), r.cypher)
        self.assertIn('x.iv_count', r.cypher)

    def test_drop_index_stops_rewriting(self):
        query = PATH + 'RETURN x.id AS person, count(*) AS n'
        self.assertTrue(self.mw.execute(query).rewritten)
        self.assertEqual(self.mw.drop_index('Person(iv_count)'), 1)
        self.assertEqual(self.mw.execute(query).reason, 'no_index_selected')

    def test_unrelated_write_and_invalid_queries_pass_through(self):
        for q in ('MATCH (x:Person) RETURN x.id AS id ORDER BY id LIMIT 3', "CREATE (:Person {id: 1})",
                  'MATCH (x:Nope) RETURN x'):
            r = self.mw.execute(q)
            self.assertFalse(r.rewritten)
            self.assertEqual(r.cypher, q)

    def test_failed_index_leaves_schema_untouched(self):
        before = self.mw.schema.to_yaml()
        r = self.mw.execute('FILL Nope(x) FROM (MATCH (a:Person) RETURN a.id, count(*))')
        self.assertFalse(r.ok)
        self.assertEqual(self.mw.schema.to_yaml(), before)

    def test_verifier_detects_a_wrong_print(self):
        response = self.mw.engine.rewrite(PATH + 'RETURN x.id AS person, count(*) AS n')
        printed = self.mw.printer.print(response['ir'])
        wrong = printed.cypher.replace('<> 0', '<> 1')
        rebound = self.mw.engine.bind(wrong)['ir']
        self.assertIsNotNone(first_difference(canonical(response['ir'], printed.entity_names),
                                              canonical(rebound, raw_names(rebound))))

    def test_build_statements_kuzu(self):
        built = self.mw.build_statements('Person(iv_count)')
        self.assertEqual(len(built), 1)
        b = built[0]
        self.assertTrue(b['ok'], b)
        self.assertEqual(b['ddl'], ['ALTER TABLE Person ADD IF NOT EXISTS iv_count INT64'])
        self.assertIn('SET', b['fill_statements'][0])
        # key `a.id` is the primary key of the target: the node itself is carried
        self.assertIn('WITH a, count(*) AS cico_v0 SET a.iv_count = cico_v0', b['fill_statements'][0])

    def test_build_statements_neo4j(self):
        mw = IndexMiddleware(self.mw.schema, profile=neo4j_profile(PROFILE_DIR / 'hetionet-neo4j.yaml'))
        try:
            self.assertTrue(mw.execute('ALTER TABLE City ADD n INT64 DEFAULT 0').ok)
            self.assertTrue(mw.execute('FILL City(n) FROM (MATCH (a:Person)-[:LIVES_IN]->(b:City)'
                                       '-[:HAS_RESTAURANT]->(c:Restaurant) RETURN b.id, count(*))').ok)
            b = mw.build_statements('City(n)')[0]
            self.assertEqual(b['ddl'], [])  # schemaless
            self.assertIn('SET cico_n.n = 0', b['reset'][0])
            # one MATCH per relationship (no Neo4j uniqueness), batched writes
            self.assertIn('MATCH (a:Person)-[', b['fill_statements'][0])
            self.assertIn(' MATCH (b)-[', b['fill_statements'][0])
            self.assertIn('IN TRANSACTIONS OF', b['fill_statements'][0])
        finally:
            mw.close()

    def test_parameter_specialization(self):
        self.assertTrue(self.mw.execute('ALTER TABLE City ADD m INT64 DEFAULT 0').ok)
        self.assertTrue(self.mw.execute('FILL City(m) FROM (MATCH (b:City)-[:HAS_RESTAURANT]->(c:Restaurant) '
                                        'WHERE c.id > 10 RETURN b.id, count(*))').ok)
        query = 'MATCH (b:City)-[:HAS_RESTAURANT]->(c:Restaurant) WHERE c.id > $t RETURN b.id AS id, count(*) AS n'
        r = self.mw.execute(query, parameters=dict(t=10))
        self.assertTrue(r.rewritten, r.detail)
        self.assertEqual(r.specialized, ['t'])
        self.assertIn('b.m', r.cypher)
        r = self.mw.execute(query, parameters=dict(t=11))  # another value: the FILL does not apply
        self.assertFalse(r.rewritten)

    def test_verifier_merges_consecutive_matches(self):
        a = self.mw.engine.bind(PATH + 'RETURN count(*) AS n')['ir']
        b = self.mw.engine.bind('MATCH (x:Person)-[:LIVES_IN]->(y:City) MATCH (y)-[:HAS_RESTAURANT]->(z:Restaurant) '
                                'RETURN count(*) AS n')['ir']
        self.assertIsNone(first_difference(canonical(a, raw_names(a)), canonical(b, raw_names(b))))

    def test_http_server(self):
        from http.server import ThreadingHTTPServer
        from cico.server import make_handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.mw))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            request = urllib.request.Request(url + '/statement', method='POST', data=json.dumps(
                dict(cypher=PATH + 'RETURN count(*) AS n')).encode())
            body = json.loads(urllib.request.urlopen(request).read())
            self.assertTrue(body['rewritten'], body)
            indexes = json.loads(urllib.request.urlopen(url + '/indexes').read())
            self.assertIn('Person(iv_count)', indexes['fills'])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
