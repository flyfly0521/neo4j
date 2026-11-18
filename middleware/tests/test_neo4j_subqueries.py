"""Integration tests for CALL boundaries and parameter-specific declarations."""
import datetime
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile, tokenize, render
from cico.normalize import normalize_temporal_comparisons
from cico.schema import Schema
from test_middleware import schema_yaml

ROOT = Path(__file__).resolve().parents[1]


class SubqueryTests(unittest.TestCase):
    def middleware(self):
        mw = IndexMiddleware(Schema.from_text(schema_yaml()),
                             profile=neo4j_profile(ROOT / 'profiles/hetionet-neo4j.yaml'))
        self.addCleanup(mw.close)
        mw.register_neo4j_index('''MATCH (a:Person)
            OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)-[:HAS_RESTAURANT]->(c:Restaurant)
            WITH a, count(c) AS n SET a.restaurants = n''')
        return mw

    def test_correlated_call_both_import_syntaxes(self):
        for opening in ('CALL (a) {', 'CALL { WITH a'):
            mw = self.middleware()
            query = f'''MATCH (a:Person) {opening}
                OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)-[:HAS_RESTAURANT]->(c:Restaurant)
                RETURN count(c) AS n }} RETURN a.id, n'''
            result = mw.execute(query)
            self.assertTrue(result.rewritten, result.to_dict())
            self.assertIn('CALL', result.cypher)
            self.assertIn('restaurants', result.cypher)
            self.assertNotIn('cico_in_', result.cypher)

    def test_call_union_keeps_each_branch_and_output_names(self):
        mw = self.middleware()
        query = '''MATCH (a:Person) CALL (a) {
            OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)-[:HAS_RESTAURANT]->(c:Restaurant)
            RETURN count(c) AS n
            UNION ALL RETURN -1 AS n
        } RETURN a.id, n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('UNION ALL', result.cypher)
        self.assertIn('RETURN -1 AS n', result.cypher)

    def test_nested_write_keeps_entire_statement(self):
        mw = self.middleware()
        query = '''MATCH (a:Person) CALL (a) {
            OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)-[:HAS_RESTAURANT]->(c:Restaurant)
            WITH a, count(c) AS n SET a.x = n RETURN n
        } RETURN a.id, n'''
        result = mw.execute(query)
        self.assertFalse(result.rewritten)
        self.assertEqual(result.reason, 'not_read_only')
        self.assertEqual(result.cypher, query)

    def test_parameter_index_is_bound_to_literal_values(self):
        mw = self.middleware()
        script = '''MATCH (a:Person) OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)
            WHERE b.id < $limit WITH a, count(b) AS n SET a.smallCities = n'''
        mw.register_neo4j_index(script, parameters={'limit': 20})
        text = next(fills[0] for name, fills in mw.fills.items() if 'smallCities' in name)
        self.assertNotIn('$limit', text)
        self.assertIn('20', text)
        result = mw.execute('''MATCH (a:Person) OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)
            WHERE b.id < $limit RETURN a.id, count(b) AS n''', parameters={'limit': 20})
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('limit', result.specialized)
        other = mw.execute('''MATCH (a:Person) OPTIONAL MATCH (a)-[:LIVES_IN]->(b:City)
            WHERE b.id < $limit RETURN a.id, count(b) AS n''', parameters={'limit': 21})
        self.assertFalse(other.rewritten)

    def test_count_relationship_normalization_preserves_distinct(self):
        frontend = self.middleware().frontend
        plain = frontend.to_internal('MATCH (a:Person)-[r:LIVES_IN]->(:City) RETURN count(r)')
        distinct = frontend.to_internal('MATCH (a:Person)-[r:LIVES_IN]->(:City) RETURN count(DISTINCT r)')
        self.assertIn('count(cico_count_node)', plain)
        self.assertIn('count(DISTINCT r)', distinct)
        reused = frontend.to_internal('''MATCH (a:Person)-[r:LIVES_IN]->(:City)
            WITH count(r) AS n MATCH (b:Person)-[r:LIVES_IN]->(:City) RETURN count(r)''')
        self.assertNotIn('cico_count_node', reused)

    def test_comparison_normalization_ignores_strings_and_arithmetic(self):
        before = "MATCH (p:Person) WHERE TIMESTAMP('2012-01-01') <= p.d RETURN p.id"
        self.assertIn("p.d >= TIMESTAMP('2012-01-01')", normalize_temporal_comparisons(before))
        text = 'RETURN "TIMESTAMP(\'2012-01-01\') <= p.d"'
        self.assertEqual(normalize_temporal_comparisons(text), text)
        text = "MATCH (p:Person) WHERE TIMESTAMP('2012-01-01') <= p.d + 1 RETURN p.id"
        self.assertEqual(normalize_temporal_comparisons(text), text)
