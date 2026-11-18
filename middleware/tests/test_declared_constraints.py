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
    if constraints:
        return neo4j_profile()
    mapping = load_mapping(ROOT / 'profiles/ldbc-snb-neo4j.yaml')
    mapping['constraints'] = []
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
        self.assertEqual(len(checks), 8)
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


if __name__ == '__main__':
    unittest.main()
