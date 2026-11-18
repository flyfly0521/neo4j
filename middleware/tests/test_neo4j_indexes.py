"""Neo4j index registration exercises the real binder and verification guard."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware, Unsupported
from cico.neo4j import neo4j_profile
from test_middleware import schema_yaml
from cico.schema import Schema


class Neo4jIndexTests(unittest.TestCase):
    def middleware(self):
        mw = IndexMiddleware(Schema.from_text(schema_yaml()), profile=neo4j_profile(Path(__file__).resolve().parents[1] / 'profiles/hetionet-neo4j.yaml'))
        self.addCleanup(mw.close)
        return mw

    def test_set_register_then_convert_and_rewrite(self):
        mw = self.middleware()
        results = mw.register_neo4j_index('''MATCH (a:Person) REMOVE a.cityId;
            MATCH (a:Person)-[:LIVES_IN]->(b:City) SET a.cityId = b.id;''')
        self.assertEqual(len(results), 1)
        result = mw.execute('MATCH (a:Person)-[:LIVES_IN]->(b:City) RETURN a.id AS personId, b.id AS cityId')
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('cityId', result.cypher)
        self.assertTrue(mw.build_statements()[0]['ok'])

    def test_batched_legacy_and_scope_call(self):
        for call in ('CALL { WITH a', 'CALL (a) {'):
            mw = self.middleware()
            result = mw.register_neo4j_index(f'''MATCH (a:Person) {call}
                MATCH (a)-[:LIVES_IN]->(b:City) SET a.cityId = b.id
                }} IN TRANSACTIONS OF 100 ROWS''')
            self.assertTrue(result[0].ok)

    def test_derived_merge(self):
        mw = self.middleware()
        results = mw.register_neo4j_index('''MATCH (a:Person)-[:LIVES_IN]->(b:City)
            MERGE (a)-[:CACHED_CITY]->(b)''')
        self.assertTrue(results[0].ok)
        self.assertIsNotNone(mw.schema.table('CACHED_CITY')[1])

    def test_unsupported_rolls_back_existing_indexes(self):
        mw = self.middleware()
        script = 'MATCH (a:Person)-[:LIVES_IN]->(b:City) SET a.cityId = b.id'
        mw.register_neo4j_index(script)
        before = mw.schema.to_yaml()
        with self.assertRaises(Unsupported):
            mw.register_neo4j_index(script + '; MATCH (a:Person) SET a.x = CASE WHEN true THEN 1 ELSE 2 END')
        self.assertEqual(mw.schema.to_yaml(), before)
        self.assertEqual(len(mw.fills), 1)
        self.assertTrue(mw.execute('MATCH (a:Person)-[:LIVES_IN]->(b:City) RETURN a.id AS personId, b.id AS cityId').rewritten)

    def test_ordinary_write_does_not_register(self):
        mw = self.middleware()
        r = mw.execute('MATCH (a:Person) SET a.x = 1')
        self.assertFalse(r.rewritten)
        self.assertEqual(len(mw.fills), 0)

    def test_registration_failure_rolls_back_schema(self):
        mw = self.middleware()
        before = mw.schema.to_yaml()
        with self.assertRaises(Exception):
            mw.register_neo4j_index('MATCH (a:Person)-[:LIVES_IN]->(b:City) SET a.cityId = b.id; '
                                    'MATCH (a:Person)-[:MISSING]->(b:City) SET a.x = b.id')
        self.assertEqual(before, mw.schema.to_yaml())
        self.assertFalse(mw.fills)

    def test_native_schema_index_is_routed_to_target(self):
        mw = self.middleware()
        text = 'CREATE INDEX person_id IF NOT EXISTS FOR (p:Person) ON (p.id)'
        result = mw.execute(text)
        self.assertEqual(result.reason, 'target_ddl')
        self.assertEqual(result.cypher, text)
        self.assertFalse(mw.fills)

    def test_root_closure_has_one_definition_and_both_partitions(self):
        root = Path(__file__).resolve().parents[1]
        with IndexMiddleware(root / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
            results = mw.register_neo4j_index("""
                MATCH (p:Post) CALL (p) { MERGE (p)-[:ROOT_POST]->(p) } IN TRANSACTIONS;
                MATCH (c:Comment) CALL (c) {
                  MATCH (c)-[:REPLY_OF*1..]->(p:Post)
                  WITH DISTINCT c, p MERGE (c)-[:ROOT_POST]->(p)
                } IN TRANSACTIONS;
            """)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(mw.fills[results[0].index_id]), 1)
            self.assertEqual(len(mw.schema.table('ROOT_POST')[1]['vertex_type_pair_relations']), 2)
            self.assertTrue(mw.build_statements()[0]['ok'])
