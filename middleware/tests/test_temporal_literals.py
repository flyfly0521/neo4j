"""Temporal literal canonicalization: one instant, one internal spelling.

A NeuG FILL keys its window by the bound TIMESTAMP literal.  Neo4j spells
the same instant as datetime('...T...Z'), as an aware driver value, or as
$date + duration({days: n}); all of them must reach the matcher as the same
literal, and nothing outside the exact domain may be folded."""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from cico.middleware import internal_literal
from cico.neo4j import neo4j_profile
from cico.normalize import fold_constants

ROOT = Path(__file__).resolve().parents[1]
UTC = datetime.timezone.utc


class FoldTests(unittest.TestCase):
    def test_equal_instants_share_one_literal(self):
        self.assertEqual(fold_constants("m.d < timestamp('2010-06-09T00:00:00Z')"),
                         "m.d < TIMESTAMP('2010-06-09 00:00:00')")
        self.assertEqual(fold_constants("m.d < timestamp('2010-06-09T02:00:00+02:00')"),
                         "m.d < TIMESTAMP('2010-06-09 00:00:00')")
        self.assertEqual(fold_constants("m.d < timestamp('2010-06-09')"),
                         "m.d < TIMESTAMP('2010-06-09 00:00:00')")
        self.assertEqual(internal_literal(datetime.datetime(2010, 6, 9, 2, tzinfo=datetime.timezone(
            datetime.timedelta(hours=2)))), "TIMESTAMP('2010-06-09 00:00:00')")

    def test_fixed_length_durations_fold(self):
        self.assertEqual(fold_constants("TIMESTAMP('2010-06-09 00:00:00') + duration('100 days') <= m.d"),
                         "TIMESTAMP('2010-09-17 00:00:00') <= m.d")
        self.assertEqual(fold_constants("m.d < TIMESTAMP('2010-06-09 00:00:00') - duration('1 days 2 hours')"),
                         "m.d < TIMESTAMP('2010-06-07 22:00:00')")

    def test_outside_the_domain_nothing_folds(self):
        for text in ("m.d < TIMESTAMP('2010-06-09 00:00:00') + duration('1 months')",   # calendar unit
                     "m.d < timestamp('2010-06-09T00:00[Europe/Berlin]')",              # named zone (DST)
                     "m.d < TIMESTAMP('2010-06-09 00:00:00') + duration('1 days') * 2",  # not a sum
                     "RETURN 'timestamp(x)'"):                                          # inside a string
            self.assertEqual(fold_constants(text), text)


class NeuGWindowFillTests(unittest.TestCase):
    """BI2 shape: Neo4j query, NeuG FILL with literal window bounds."""

    def setUp(self):
        self.mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile())
        self.addCleanup(self.mw.close)
        self.mw.execute('ALTER TABLE TAG ADD IF NOT EXISTS w1 INT64 DEFAULT 0')
        self.mw.register_index(["FILL TAG(w1) FROM (MATCH (m:POST:COMMENT)-[:HASTAG]->(t:TAG) "
                                "WHERE m.creationDate >= TIMESTAMP('2010-06-09 00:00:00') "
                                "AND m.creationDate < TIMESTAMP('2010-09-17 00:00:00') RETURN t.id, count(m))"],
                               input_dialect='neug')
        self.query = '''MATCH (tag:Tag)-[:HAS_TYPE]->(:TagClass {name: $tagClass})
OPTIONAL MATCH (message1:Message)-[:HAS_TAG]->(tag)
  WHERE $date <= message1.creationDate AND message1.creationDate < $date + duration({days: 100})
WITH tag, count(message1) AS countWindow1
RETURN tag.name, countWindow1'''

    def test_parameter_window_is_specialized(self):
        result = self.mw.execute(self.query, parameters={
            'date': datetime.datetime(2010, 6, 9, tzinfo=UTC), 'tagClass': 'Single'})
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertEqual(result.specialized, ['date'])
        self.assertIn('w1', result.cypher)
        self.assertIn('$tagClass', result.cypher)

    def test_literal_window_rewrites(self):
        query = self.query.replace('$date + duration({days: 100})', "datetime('2010-09-17T00:00:00Z')") \
            .replace('$date <=', "datetime('2010-06-09T00:00:00Z') <=")
        result = self.mw.execute(query, parameters={'tagClass': 'Single'})
        self.assertTrue(result.rewritten, result.to_dict())

    def test_other_window_is_not_rewritten(self):
        for date in (datetime.datetime(2010, 6, 10, tzinfo=UTC),        # shifted window: (M_F) fails
                     datetime.datetime(2010, 6, 9, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))):
            result = self.mw.execute(self.query, parameters={'date': date, 'tagClass': 'Single'})
            self.assertFalse(result.rewritten, result.to_dict())
            self.assertEqual(result.cypher, self.query)


if __name__ == '__main__':
    unittest.main()
