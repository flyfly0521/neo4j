"""Real compiler regressions for the registered Neo4j BI index failures."""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile
from cico.schema import Schema
from test_middleware import schema_yaml

ROOT = Path(__file__).resolve().parents[1]


class BIRewriteFixTests(unittest.TestCase):
    def middleware(self, profile='neo4j'):
        mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=profile)
        self.addCleanup(mw.close)
        return mw

    def test_optional_source_with_union_partitions_and_interval(self):
        mw = self.middleware()
        script = '''MATCH(t:Tag) SET t.windowCount=0;
            MATCH(m:Message)-[:HAS_TAG]->(t:Tag)
            WHERE m.creationDate >= $date AND m.creationDate < $date+duration('P100D')
            WITH t,count(m) AS n SET t.windowCount=n'''
        params = {'date': datetime.datetime(2010, 6, 9)}
        mw.register_neo4j_index(script, parameters=params)
        query = '''MATCH(t:Tag) OPTIONAL MATCH(m:Message)-[:HAS_TAG]->(t)
            WHERE m.creationDate >= $date AND m.creationDate < $date+duration('P100D')
            RETURN t.id AS tagId,count(m) AS n'''
        result = mw.execute(query, parameters=params)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('TAG(windowCount)', result.indexes)
        self.assertIn('date', result.specialized)
        wrong_window = mw.execute(query, parameters={'date': datetime.datetime(2010, 6, 10)})
        self.assertFalse(wrong_window.rewritten, wrong_window.to_dict())
        self.assertEqual(wrong_window.cypher, query)

    def test_total_distinct_date_count_and_nonzero_mandatory_guard(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(p:Person) SET p.dayCount=0;
            MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
            WHERE t.name='tag' AND date(m.creationDate)=date($date)
            WITH p,count(DISTINCT m) AS n SET p.dayCount=n''',
            parameters={'date': datetime.datetime(2012, 7, 26)})
        pattern = '''(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
            WHERE t.name='tag' AND date(m.creationDate)=date($date)'''
        for opening in ('MATCH(p:Person) OPTIONAL MATCH ', 'MATCH '):
            query = opening + pattern + ' RETURN p.id AS personId,count(DISTINCT m) AS n'
            result = mw.execute(query, parameters={'date': datetime.datetime(2012, 7, 26)})
            self.assertTrue(result.rewritten, result.to_dict())
            self.assertIn('PERSON(dayCount)', result.indexes)
            if opening == 'MATCH ':
                self.assertRegex(result.cypher, r'dayCount\s*<>\s*0')
        # The same total source cannot count the outer anchor: its padded row
        # contributes one for p but zero for the fresh optional witness m.
        bad = mw.execute('MATCH(p:Person) OPTIONAL MATCH ' + pattern +
                         ' RETURN p.id AS personId,count(DISTINCT p) AS n',
                         parameters={'date': datetime.datetime(2012, 7, 26)})
        self.assertFalse(bad.rewritten, bad.to_dict())

    def test_post_aggregate_filter_keeps_rewritten_count(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)
            WITH p,count(DISTINCT m) AS n SET p.messages=n''')
        result = mw.execute('''MATCH(p:Person)<-[:HAS_CREATOR]-(m:Message)
            WITH p,count(DISTINCT m) AS n WHERE n>0 RETURN p.id AS personId,n''')
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('messages', result.cypher)
        self.assertIn('WHERE', result.cypher)

    def test_optional_distinct_source_cannot_hide_extra_outer_filter(self):
        mw = self.middleware('neug')
        source = '''MATCH(p:PERSON) WHERE p.id>10
            OPTIONAL MATCH(p)<-[:HASCREATOR]-(m:POST)
            RETURN p.id,count(DISTINCT m)'''
        mw.execute('FILL PERSON(messages) FROM (' + source + ')')
        query = '''MATCH(p:PERSON) OPTIONAL MATCH(p)<-[:HASCREATOR]-(m:POST)
            RETURN p.id AS personId,count(DISTINCT m) AS n'''
        self.assertFalse(mw.execute(query).rewritten)

    def test_nullable_distinct_property_is_not_a_presence_proof(self):
        mw = self.middleware('neug')
        source = '''MATCH(p:PERSON) OPTIONAL MATCH(p)<-[:HASCREATOR]-(m:POST)
            RETURN p.id,count(DISTINCT m.content)'''
        mw.execute('FILL PERSON(texts) FROM (' + source + ')')
        query = '''MATCH(p:PERSON)<-[:HASCREATOR]-(m:POST)
            RETURN p.id AS personId,count(DISTINCT m.content) AS n'''
        self.assertFalse(mw.execute(query).rewritten)

    def test_opaque_apoc_prefix_allows_verified_later_country_rewrite(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(p:Person)-[:IS_LOCATED_IN]->(:City)
            -[:IS_PART_OF]->(c:Country) SET p.countryName=c.name''')
        query = '''MATCH(start:Person {id:$personId})
            CALL apoc.path.subgraphNodes(start,{relationshipFilter:'KNOWS',maxLevel:$distance}) YIELD node
            WITH collect(DISTINCT node) AS people
            WITH [p IN people WHERE p.id<>$personId] AS candidates
            UNWIND candidates AS p
            MATCH(p)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(c:Country)
            WHERE c.name=$country
            MATCH(p)<-[:HAS_CREATOR]-(m:Message)
            RETURN p.id AS personId,count(DISTINCT m) AS n'''
        params = {'personId': 1, 'distance': 4, 'country': 'China'}
        result = mw.execute(query, parameters=params)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('PERSON(countryName)', result.indexes)
        self.assertIn('apoc.path.subgraphNodes', result.cypher)
        self.assertIn('[p IN people WHERE p.id<>$personId]', result.cypher)
        self.assertNotIn('cico_in_', result.cypher)
        # A projection-copy FILL may collapse duplicate witnesses. Its hidden
        # row state cannot justify leaving an unrelated ordinary COUNT alone.
        ordinary = mw.execute(query.replace('count(DISTINCT m)', 'count(m)'), parameters=params)
        self.assertFalse(ordinary.rewritten, ordinary.to_dict())
        # Traversing to a different label domain cannot inherit seed types.
        other = mw.execute(query.replace("relationshipFilter:'KNOWS'",
                                         "relationshipFilter:'IS_LOCATED_IN'"), parameters=params)
        self.assertFalse(other.rewritten, other.to_dict())
        self.assertEqual(other.cypher, query.replace("relationshipFilter:'KNOWS'",
                                                   "relationshipFilter:'IS_LOCATED_IN'"))

    def test_procedure_type_contract_is_independent_of_workload_and_names(self):
        profile = neo4j_profile(ROOT / 'profiles/hetionet-neo4j.yaml')
        mw = IndexMiddleware(Schema.from_text(schema_yaml()), profile=profile)
        self.addCleanup(mw.close)
        mw.execute('CREATE REL TABLE PEER(FROM Person TO Person)')
        mw.frontend.procedure_types['extension.traverse'] = {
            'arity': 2, 'config_argument': 1, 'type_preserving_options': ['edge', 'limit'],
            'outputs': {'vertex': {'kind': 'reachable_node', 'seed_argument': 0,
                                   'relationship_option': 'edge'}}}
        mw.register_neo4j_index('MATCH(a:Person)-[:LIVES_IN]->(b:City) SET a.townKey=b.id')
        query = '''MATCH(origin:Person {id:$who}) WITH origin AS renamed
            CALL extension.traverse(renamed,{edge:'PEER',limit:$depth}) YIELD vertex AS picked
            WITH collect(picked) AS things UNWIND things AS outerNode
            MATCH(outerNode)-[:LIVES_IN]->(destination:City)
            RETURN outerNode.id AS who,destination.id AS town'''
        result = mw.execute(query, parameters={'who': 5, 'depth': 2})
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('townKey', result.cypher)
        self.assertIn('extension.traverse', result.cypher)
        self.assertNotIn('cico_in_', result.cypher)

    def test_patch_preserves_current_source_updates_and_fails_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / 'kernel.cpp'
            source.write_text('// newer upstream fix\nold rule\n')
            patch = temp / 'rule.patch'
            patch.write_text('diff --git a/kernel.cpp b/kernel.cpp\n'
                             '--- a/kernel.cpp\n+++ b/kernel.cpp\n'
                             '@@ -1 +1 @@\n-old rule\n+new rule\n')
            output = temp / 'prepared.cpp'
            command = [sys.executable, str(ROOT / 'engine/prepare_patch.py'),
                       '--neug-source', str(temp), '--source', 'kernel.cpp',
                       '--patch', str(patch), '--output', str(output)]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(output.read_text(), '// newer upstream fix\nnew rule\n')
            self.assertIn('base_sha256', json.loads(output.with_suffix('.json').read_text()))
            source.write_text('incompatible source\n')
            failed = subprocess.run(command, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(output.read_text(), '// newer upstream fix\nnew rule\n')


if __name__ == '__main__':
    unittest.main()
