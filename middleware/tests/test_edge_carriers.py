"""Relationship-carrier existence rule (edge_carriers.py): where it applies and where it must not."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile

ROOT = Path(__file__).resolve().parents[1]
CASE1 = '''FILL KNOWS(c1_fwd) FROM (MATCH (pA:PERSON)-[knows:KNOWS]->(pB:PERSON) WITH pA, pB
MATCH (pA)<-[:HASCREATOR]-(c:COMMENT)-[:REPLYOF]->(:COMMENT:POST)-[:HASCREATOR]->(pB)
RETURN pA.id, pB.id, count(c) AS cnt)'''
CASE1_REV = CASE1.replace('c1_fwd', 'c1_rev').replace('(pA)<-', '(TMP)<-').replace('->(pB)', '->(pA)') \
    .replace('(TMP)<-', '(pB)<-')
HEAD = '''MATCH (p1:Person {id: $a}), (p2:Person {id: $b})
WITH p1, p2, 0 AS score
'''
EXISTS = '''OPTIONAL MATCH (p1)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(p2)
'''
TAIL = 'RETURN p1.id, p2.id, score'


class EdgeCarrierTests(unittest.TestCase):
    def setUp(self):
        self.mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile())
        self.addCleanup(self.mw.close)
        for column in ('c1_fwd', 'c1_rev'):
            self.mw.execute(f'ALTER TABLE KNOWS ADD IF NOT EXISTS {column} INT64 DEFAULT 0')
        self.mw.register_index([CASE1], input_dialect='neug')
        self.mw.register_index([CASE1_REV], input_dialect='neug')

    def rewrite(self, middle):
        return self.mw.execute(HEAD + middle + TAIL, parameters={'a': 1, 'b': 2})

    def test_existence_reads_both_orientations(self):
        r = self.rewrite(EXISTS + 'WITH DISTINCT p1, p2, score + (CASE WHEN c IS NULL THEN 0 ELSE 4 END) AS score\n')
        self.assertTrue(r.rewritten, r.to_dict())
        self.assertIn('startNode(cico_k) = p1 THEN coalesce(cico_k.c1_fwd, 0) > 0', r.cypher)
        self.assertIn('ELSE coalesce(cico_k.c1_rev, 0) > 0', r.cypher)
        self.assertIn('WHEN cico_k IS NULL THEN EXISTS { MATCH (p1)<-[:HAS_CREATOR]-(c:Comment)', r.cypher)
        self.assertEqual(r.proofs[0]['rule'], 'relationship_carrier_existence')

    def test_swapped_pattern_uses_swapped_columns(self):
        swapped = EXISTS.replace('(p1)<-', '(TMP)<-').replace('->(p2)', '->(p1)').replace('(TMP)<-', '(p2)<-')
        r = self.rewrite(swapped + 'WITH DISTINCT p1, p2, score + (CASE WHEN c IS NOT NULL THEN 1 ELSE 0 END) AS score\n')
        self.assertTrue(r.rewritten, r.to_dict())
        self.assertIn('startNode(cico_k) = p1 THEN coalesce(cico_k.c1_rev, 0) > 0', r.cypher)

    def test_not_applicable(self):
        cases = {
            'multiplicity visible (no DISTINCT)':
                EXISTS + 'WITH p1, p2, score + (CASE WHEN c IS NULL THEN 0 ELSE 4 END) AS score\n',
            'aggregate sees multiplicities':
                EXISTS + 'WITH DISTINCT p1, p2, score + count(c) AS score\n',
            'fresh variable projected':
                EXISTS + 'WITH DISTINCT p1, p2, c AS score\n',
            'filter the carrier does not store':
                EXISTS.rstrip('\n') + ' WHERE c.length > 3\n'
                + 'WITH DISTINCT p1, p2, score + (CASE WHEN c IS NULL THEN 0 ELSE 4 END) AS score\n',
            'different pattern':
                EXISTS.replace('[:REPLY_OF]', '[:REPLY_OF*1..2]')
                + 'WITH DISTINCT p1, p2, score + (CASE WHEN c IS NULL THEN 0 ELSE 4 END) AS score\n',
        }
        for name, middle in cases.items():
            with self.subTest(name):
                r = self.rewrite(middle)
                self.assertNotIn('cico_k', r.cypher, name)


if __name__ == '__main__':
    unittest.main()
