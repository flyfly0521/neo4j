"""Local rules under the wiki's binding/interface/multiplicity obligations."""
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from cico import IndexMiddleware
from cico.binding_values import finite_inputs
from cico.neo4j import neo4j_profile
from cico.schema import Schema
from test_middleware import schema_yaml

ROOT = Path(__file__).resolve().parents[1]


def theory_schema():
    return schema_yaml().replace('Person', 'ProofPerson').replace('City', 'ProofCity').replace(
        'Restaurant', 'ProofRestaurant').replace('LIVES_IN', 'PROOF_LINK').replace(
        'HAS_RESTAURANT', 'PROOF_NEXT')


class TheoryRewriteTests(unittest.TestCase):
    def middleware(self):
        mw = IndexMiddleware(Schema.from_text(theory_schema()),
                             profile=neo4j_profile(ROOT / 'profiles/hetionet-neo4j.yaml'))
        self.addCleanup(mw.close)
        mw.register_neo4j_index('''MATCH(a:ProofPerson) SET a.places=0;
            MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity)
            WITH a,count(b) AS n SET a.places=n''')
        return mw

    def test_size_count_exists_and_outer_arithmetic(self):
        mw = self.middleware()
        for expression in ('size([(a)-[:PROOF_LINK]->(b:ProofCity)|b])',
                           'COUNT { MATCH(a)-[:PROOF_LINK]->(b:ProofCity) }',
                           'EXISTS { MATCH(a)-[:PROOF_LINK]->(b:ProofCity) }',
                           '7 + 2 * size([(a)-[:PROOF_LINK]->(b:ProofCity)|b])'):
            result = mw.execute('MATCH(a:ProofPerson) RETURN a.id AS who,' + expression + ' AS n')
            self.assertTrue(result.rewritten, result.to_dict())
            self.assertIn('places', result.cypher)
            self.assertEqual(result.proofs[0]['anchors'], ['a'])
            self.assertNotIn('cico_in_', result.cypher)

    def test_outer_duplicate_rows_and_null_anchor_guard_remain(self):
        mw = self.middleware()
        query = '''UNWIND [1,1] AS duplicate OPTIONAL MATCH(a:ProofPerson)
            RETURN duplicate,size([(a)-[:PROOF_LINK]->(b:ProofCity)|b]) AS n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('UNWIND [1,1]', result.cypher)
        self.assertIn('a IS NULL THEN 0', result.cypher)

    def test_list_concatenation_is_only_a_type_probe(self):
        mw = self.middleware()
        query = '''MATCH(a:ProofPerson) WITH collect(a) AS xs
            MATCH(b:ProofPerson) WITH xs,xs + collect(b) AS things
            UNWIND things AS x
            RETURN x.id AS who,size([(x)-[:PROOF_LINK]->(c:ProofCity)|c]) AS n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('xs + collect(b)', result.cypher)
        self.assertIn('UNWIND things AS x', result.cypher)

    def test_hidden_correlated_anchor_cannot_be_discarded(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(p:ProofPerson) SET p.paths=0;
            MATCH(p:ProofPerson)-[:PROOF_LINK]->(c:ProofCity)-[:PROOF_NEXT]->(r:ProofRestaurant)
            WITH p,count(c) AS n SET p.paths=n''')
        query = '''MATCH(p:ProofPerson),(r:ProofRestaurant)
            RETURN size([(p)-[:PROOF_LINK]->(c:ProofCity)-[:PROOF_NEXT]->(r:ProofRestaurant)|c]) AS n'''
        result = mw.execute(query)
        self.assertFalse(result.rewritten, result.to_dict())
        self.assertEqual(result.cypher, query)

    def test_projection_errors_and_extra_filters_are_not_erased(self):
        mw = self.middleware()
        for expression in ('size([(a)-[:PROOF_LINK]->(b:ProofCity)|1/0])',
                           'size([(a)-[:PROOF_LINK]->(b:ProofCity) WHERE b.id>10|b])',
                           'size([(a)-[:PROOF_LINK*1..3]->(b:ProofCity)|b])'):
            query = 'MATCH(a:ProofPerson) RETURN ' + expression + ' AS n'
            result = mw.execute(query)
            self.assertFalse(result.rewritten, result.to_dict())
            self.assertEqual(result.cypher, query)

    def test_alias_scope_uses_current_binding(self):
        mw = self.middleware()
        query = '''MATCH(a:ProofCity) WITH a AS discarded MATCH(a:ProofPerson)
            RETURN size([(a)-[:PROOF_LINK]->(c:ProofCity)|c]) AS n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertEqual(result.proofs[0]['anchors'], ['a'])

    def test_quoted_variable_names_do_not_change_matching(self):
        mw = self.middleware()
        query = '''MATCH(`odd anchor`:ProofPerson)
            RETURN size([(`odd anchor`)-[:PROOF_LINK]->(`odd city`:ProofCity)|`odd city`]) AS n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertEqual(result.proofs[0]['anchors'], ['odd anchor'])

    def test_probe_parameters_are_independent_of_user_parameters(self):
        mw = self.middleware()
        query = '''MATCH(p:ProofPerson) WITH p,p.id AS k
            OPTIONAL MATCH(p)-[:PROOF_LINK]->(c:ProofCity) WHERE k=$cico_in_k
            RETURN p.id AS who,count(c) AS n'''
        result = mw.execute(query, parameters={'cico_in_k': 1})
        self.assertTrue(result.rewritten, result.to_dict())
        # An accidental shared probe parameter would turn this into k=k and
        # erase the condition for all input bindings, violating the theorem.
        self.assertIn('k = $cico_in_k', result.cypher)
        self.assertNotIn('$cico_in_k_', result.cypher)

    def test_finite_records_preserve_correlations_and_reject_unknown_values(self):
        prefix = '''UNWIND [{x:$one,y:10},{x:$two,y:20},{x:$one,y:10}] AS record
            WITH record.x AS renamed,record.y AS threshold'''
        self.assertEqual(finite_inputs(prefix, ['renamed', 'threshold']),
                         [{'renamed': '$one', 'threshold': '10'},
                          {'renamed': '$two', 'threshold': '20'}])
        self.assertIsNone(finite_inputs('UNWIND $records AS r WITH r.x AS x', ['x']))
        self.assertIsNone(finite_inputs('WITH rand() AS x', ['x']))
        self.assertIsNone(finite_inputs('UNWIND [' + ','.join(str(i) for i in range(33)) + '] AS x', ['x']))

    def test_finite_call_uses_guards_and_keeps_unmatched_case(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(a:ProofPerson) SET a.smallPlaces=0;
            MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity) WHERE b.id<10
            WITH a,count(b) AS n SET a.smallPlaces=n''')
        query = '''UNWIND [{x:10},{x:20},{x:10},{x:null}] AS record WITH record.x AS threshold
            CALL { WITH threshold MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity)
                WHERE b.id<threshold RETURN a.id AS who,count(b) AS n }
            RETURN threshold,who,n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('UNION ALL', result.cypher)
        self.assertIn('AND NOT', result.cypher)
        self.assertIn('b.id<threshold', result.cypher)
        self.assertIn('IS NULL', result.cypher)
        self.assertIn('valueType(', result.cypher)
        self.assertEqual(result.proofs[-1]['cases'], 3)

    def test_finite_call_parameter_values_can_overlap(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(a:ProofPerson) SET a.smallPlaces=0;
            MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity) WHERE b.id<10
            WITH a,count(b) AS n SET a.smallPlaces=n''')
        query = '''UNWIND [{x:$one},{x:$two}] AS record WITH record.x AS threshold
            CALL { WITH threshold MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity)
                WHERE b.id<threshold RETURN a.id AS who,count(b) AS n }
            RETURN threshold,who,n'''
        result = mw.execute(query, parameters={'one': 10, 'two': 10})
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('AND NOT', result.cypher)
        self.assertTrue(result.specialized)

    def test_equal_numeric_values_do_not_erase_type_dependent_expressions(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(a:ProofPerson) SET a.smallPlaces=0;
            MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity) WHERE b.id<10
            WITH a,count(b) AS n SET a.smallPlaces=n''')
        query = '''UNWIND [{x:10},{x:10.0}] AS record WITH record.x AS threshold
            CALL { WITH threshold MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity)
                WHERE b.id<threshold
                RETURN a.id AS who,count(b) AS n,toString(threshold) AS label }
            RETURN threshold,who,n,label'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('valueType(threshold)', result.cypher)
        self.assertIn('toString(threshold)', result.cypher)

    def test_finite_import_in_pattern_property_map(self):
        mw = self.middleware()
        mw.register_neo4j_index('''MATCH(a:ProofPerson) SET a.somePlaces=0;
            MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity {id:5})
            WITH a,count(b) AS n SET a.somePlaces=n''')
        query = '''UNWIND [{x:5},{x:15}] AS row WITH row.x AS chosen
            CALL { WITH chosen MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity {id:chosen})
                RETURN a.id AS who,count(b) AS n } RETURN chosen,who,n'''
        result = mw.execute(query)
        self.assertTrue(result.rewritten, result.to_dict())
        self.assertIn('somePlaces', result.cypher)


if __name__ == '__main__':
    unittest.main()
