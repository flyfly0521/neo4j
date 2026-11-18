"""Declared constraints: checks, root-path uniqueness proofs, relation-carrier path substitution."""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from cico.errors import Unsupported
from cico.neo4j import load_mapping, neo4j_profile
from cico.path_carriers import substitute

ROOT = Path(__file__).resolve().parents[1]
ROOT_POST = ['DROP TABLE IF EXISTS ROOT_POST',
             'CREATE REL TABLE IF NOT EXISTS ROOT_POST(from COMMENT to POST, from POST to POST)']
FILL = '''FILL ROOT_POST() FROM (MATCH (post:POST)<-[:REPLYOF*0..]-(message:COMMENT:POST)
RETURN DISTINCT message.id, post.id)'''
TWO_PATHS = '''MATCH (m1:Message)-[:REPLY_OF*0..]->(p1:Post)<-[:CONTAINER_OF]-(f1:Forum),
      (m2:Message)-[:REPLY_OF*0..]->(p2:Post)<-[:CONTAINER_OF]-(f2:Forum)
MATCH (m1)-[:HAS_TAG]->(t:Tag)
{where}RETURN f1.id, f2.id, count(*)'''


def profile(constraints=True):
    """constraints: True (as declared), False (none) or a predicate keeping some."""
    if constraints is True:
        return neo4j_profile()
    mapping = load_mapping(ROOT / 'profiles/ldbc-snb-neo4j.yaml')
    mapping['constraints'] = [c for c in mapping['constraints'] if constraints and constraints(c)]
    handle = tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False)
    yaml.safe_dump(mapping, handle)
    handle.close()
    return neo4j_profile(handle.name)


def middleware(constraints=True):
    mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=profile(constraints))
    for s in ROOT_POST:
        mw.apply_ddl(s, input_dialect='neug')
    mw.register_index([FILL], input_dialect='neug')
    return mw


class ConstraintTests(unittest.TestCase):
    def test_every_declared_constraint_has_a_violation_count(self):
        with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
            checks = mw.constraint_checks()
        self.assertEqual(len(checks), 12)
        for check in checks:
            self.assertIn('AS violations', check['cypher'])
        self.assertIn('MATCH (n:`Message`)-[r:`REPLY_OF`]->()', checks[0]['cypher'])


class RootPathUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.fe = neo4j_profile().frontend
        with middleware() as mw:
            self.fe.set_schema(mw.schema)

    def test_overlap_contradicts_the_part_filter(self):
        out = self.fe.to_internal(TWO_PATHS.format(where='WHERE f1 <> f2 '))
        self.assertIn('REPLYOF*', out)
        self.assertEqual(self.fe.premises, {'at_most_one: (:Message)-[:REPLY_OF]->',
                                            'at_most_one: -[:CONTAINER_OF]->(:Post)'})

    def test_without_the_filter_or_constraints_it_is_refused(self):
        with self.assertRaises(Unsupported):
            self.fe.to_internal(TWO_PATHS.format(where=''))
        with self.assertRaises(Unsupported):   # an OPTIONAL clause's WHERE filters nothing
            self.fe.to_internal(TWO_PATHS.format(where='').replace('MATCH (m1)-[:HAS_TAG]->(t:Tag)\n',
                                                                    'OPTIONAL MATCH (m1)-[:HAS_TAG]->(t:Tag) WHERE f1 <> f2\n'))
        bare = profile(constraints=False).frontend
        with middleware() as mw:
            bare.set_schema(mw.schema)
        with self.assertRaises(Unsupported):
            bare.to_internal(TWO_PATHS.format(where='WHERE f1 <> f2 '))


class PathCarrierTests(unittest.TestCase):
    def test_single_path_is_substituted_in_written_direction(self):
        with middleware() as mw:
            text, used, proofs, premises = substitute(
                mw, 'MATCH (m:Message)-[:REPLY_OF*0..]->(p:Post)<-[:CONTAINER_OF]-(f:Forum) RETURN f.id, count(m)')
            self.assertIn('(m:Message)-[:ROOT_POST]->(p:Post)', text)
            self.assertEqual(used, ['ROOT_POST()'])
            self.assertEqual(premises, ['at_most_one: (:Message)-[:REPLY_OF]->'])
            text, *_ = substitute(mw, 'MATCH (p:Post)<-[:REPLY_OF*0..]-(m:Message) RETURN count(*)')
            self.assertIn('(p:Post)<-[:ROOT_POST]-(m:Message)', text)

    def test_not_substituted(self):
        with middleware() as mw:
            for query in ('MATCH (m:Message)-[:REPLY_OF*1..]->(p:Post) RETURN count(*)',      # other bounds
                          'MATCH (m:Message)-[r:REPLY_OF*0..]->(p:Post) RETURN size(r)',     # path is used
                          'MATCH (m:Message)-[:REPLY_OF*0..]->(p:Message) RETURN count(*)'):  # end may continue
                self.assertIsNone(substitute(mw, query)[0], query)
        with middleware(constraints=False) as mw:
            self.assertIsNone(substitute(mw, 'MATCH (m:Message)-[:REPLY_OF*0..]->(p:Post) RETURN count(*)')[0])

    def test_two_carriers_in_one_clause_need_a_contradiction(self):
        with middleware() as mw:
            text, *_ = substitute(mw, TWO_PATHS.format(where='WHERE f1 <> f2 '))
            self.assertEqual(text.count('ROOT_POST'), 2)
            # Without f1 <> f2 Neo4j would newly force the two ROOT_POST edges apart.
            # (The frontend already refuses that statement for the paths themselves.)
            with self.assertRaises(Unsupported):
                substitute(mw, TWO_PATHS.format(where=''))


COUNTRY_FILL = '''FILL PERSON(countryName) FROM (
MATCH (country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)
RETURN person.id, country.name)'''
LOCATED = '''MATCH (p:Person) WITH collect(p) AS people
UNWIND people AS person
MATCH (person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(:Country {name: $country}),
      (person)<-[:HAS_CREATOR]-(message:Message)
RETURN person.id, count(DISTINCT message) AS n'''


class ImpliedLabelTests(unittest.TestCase):
    def run_with(self, constraints, query=LOCATED):
        mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=profile(constraints))
        self.addCleanup(mw.close)
        mw.apply_ddl("ALTER TABLE PERSON ADD IF NOT EXISTS countryName STRING DEFAULT ''", input_dialect='neug')
        mw.register_index([COUNTRY_FILL], input_dialect='neug')
        return mw.execute(query, parameters={'country': 'China'})

    def test_types_implied_and_carrier_functional_and_total(self):
        r = self.run_with(True)
        self.assertTrue(r.rewritten, r.to_dict())
        self.assertIn('person.countryName = $country', r.cypher)
        self.assertTrue(r.route.startswith('declared_constraints:'))
        for premise in ('endpoint_labels: (:Person)-[:IS_LOCATED_IN]->(:City)',
                        'endpoint_labels: (:City)-[:IS_PART_OF]->(:Country)',
                        'at_most_one: (:Person)-[:IS_LOCATED_IN]->', 'at_least_one: (:Person)-[:IS_LOCATED_IN]->',
                        'at_most_one: (:City)-[:IS_PART_OF]->', 'at_least_one: (:City)-[:IS_PART_OF]->'):
            self.assertIn(premise, r.premises)

    def test_each_missing_premise_refuses(self):
        for kind in ('endpoint_labels', 'at_most_one', 'at_least_one'):
            with self.subTest(kind):
                r = self.run_with(lambda c, kind=kind: c['kind'] != kind)
                self.assertFalse(r.rewritten, r.to_dict())

    def test_unlabelled_source_implies_nothing(self):
        query = LOCATED.replace('MATCH (p:Person) WITH collect(p) AS people\nUNWIND people AS person\n', '')
        r = self.run_with(True, query)
        self.assertFalse(r.rewritten, r.to_dict())

    def test_typed_labels_never_follow_from_tables(self):
        c = neo4j_profile().frontend.constraints
        self.assertIsNone(c.at_most_one_out('IS_PART_OF', {'PLACE'}))          # a PLACE need not be a City
        self.assertIsNotNone(c.at_most_one_out('IS_PART_OF', {'PLACE'}, {'City'}))
        self.assertIsNotNone(c.at_most_one_out('REPLY_OF', {'COMMENT'}))        # Comment rows are Messages


TRIANGLE = '''MATCH (a:Person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country:Country {name: $country}),
      (a)-[:KNOWS]-(b:Person)
WITH DISTINCT country, a, b
MATCH (b)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)
WITH DISTINCT country, a, b
RETURN count(*) AS n'''


class KeyIdentityTests(ImpliedLabelTests):
    def test_key_node_deferred_and_bound_endpoint_compared_by_key(self):
        r = self.run_with(True, TRIANGLE)
        self.assertTrue(r.rewritten, r.to_dict())
        self.assertIn('a.countryName = $country', r.cypher)
        self.assertIn('WITH DISTINCT a, b MATCH (country:Country) WHERE (country.name = $country)', r.cypher)
        self.assertIn('(b.countryName = country.name) AND (country:Country)', r.cypher)
        for premise in ('unique_property: Country.name', 'required_property: Country.name'):
            self.assertIn(premise, r.premises)

    def test_without_a_declared_key_identity_stays(self):
        for kind in ('unique_property', 'required_property'):
            with self.subTest(kind):
                r = self.run_with(lambda c, kind=kind: c['kind'] != kind, TRIANGLE)
                self.assertNotIn('b.countryName', r.cypher)
                self.assertNotIn('a.countryName', r.cypher)


if __name__ == '__main__':
    unittest.main()
