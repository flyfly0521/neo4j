"""Tiny Neo4j equivalence checks in ONE ROLLED-BACK transaction.

Does not execute BI queries or change the SF1 dataset. Unique temporary
labels isolate the fixture; node creation, index values and all reads share
one transaction which is rolled back on success or failure.
"""
import argparse
import json
import logging
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile
from cico.schema import Schema
from cico.statements import split_script
from test_theory_rewrites import theory_schema

ROOT = Path(__file__).resolve().parents[1]
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)


def run(uri, output):
    suffix = uuid.uuid4().hex[:10]
    mapping = {n: n + '_' + suffix for n in
               ('ProofPerson', 'ProofCity', 'ProofRestaurant', 'PROOF_LINK', 'PROOF_NEXT')}

    def names(text):
        for old, new in mapping.items():
            text = text.replace(old, new)
        return text

    script = names('''MATCH(a:ProofPerson) SET a.places=0,a.smallPlaces=0;
        MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity)
        WITH a,count(b) AS n SET a.places=n;
        MATCH(a:ProofPerson)-[:PROOF_LINK]->(b:ProofCity) WHERE b.id<10
        WITH a,count(b) AS n SET a.smallPlaces=n;''')
    cases = [
        ('size', 'MATCH(p:ProofPerson) RETURN p.id AS who,size([(p)-[:PROOF_LINK]->(c:ProofCity)|c]) AS n', {}),
        ('count', 'MATCH(p:ProofPerson) RETURN p.id AS who,COUNT { MATCH(p)-[:PROOF_LINK]->(c:ProofCity) } AS n', {}),
        ('exists', 'MATCH(p:ProofPerson) RETURN p.id AS who,EXISTS { MATCH(p)-[:PROOF_LINK]->(c:ProofCity) } AS yes', {}),
        ('outer_sum', 'MATCH(p:ProofPerson) RETURN sum(size([(p)-[:PROOF_LINK]->(c:ProofCity)|c])) AS total', {}),
        ('duplicates', 'UNWIND [1,1] AS duplicate MATCH(p:ProofPerson) RETURN duplicate,p.id AS who,size([(p)-[:PROOF_LINK]->(c:ProofCity)|c]) AS n', {}),
        ('null_anchor', '''MATCH(c:ProofCity {id:15}) OPTIONAL MATCH(c)<-[:PROOF_LINK]-(p:ProofPerson)
            WHERE p.id=-999 RETURN p.id AS who,size([(p)-[:PROOF_LINK]->(c2:ProofCity)|c2]) AS n''', {}),
        ('list_concat', '''MATCH(p:ProofPerson) WITH collect(p) AS xs MATCH(q:ProofPerson)
            WITH xs,xs + collect(q) AS combined UNWIND combined AS p
            RETURN p.id AS who,size([(p)-[:PROOF_LINK]->(c:ProofCity)|c]) AS n''', {}),
        ('independent_probe_parameters', '''MATCH(p:ProofPerson) WITH p,p.id AS k
            OPTIONAL MATCH(p)-[:PROOF_LINK]->(c:ProofCity) WHERE k=$cico_in_k
            RETURN p.id AS who,count(c) AS n''', {'cico_in_k': 1}),
    ]
    grouped = '''UNWIND [{x:10},{x:20},{x:10},{x:null}] AS record WITH record.x AS threshold
        CALL { WITH threshold MATCH(p:ProofPerson)-[:PROOF_LINK]->(c:ProofCity)
            WHERE c.id<threshold RETURN p.id AS who,count(c) AS n }
        RETURN threshold,who,n'''
    cases.append(('finite_duplicates_and_null', grouped, {}))
    parameterized = grouped.replace('[{x:10},{x:20},{x:10},{x:null}]', '[{x:$one},{x:$two}]')
    cases.append(('overlapping_parameter_cases', parameterized, {'one': 10, 'two': 10}))
    cases.append(('different_parameter_cases', parameterized, {'one': 10, 'two': 20}))
    typed = '''UNWIND [{x:10},{x:10.0}] AS record WITH record.x AS threshold
        CALL { WITH threshold MATCH(p:ProofPerson)-[:PROOF_LINK]->(c:ProofCity)
            WHERE c.id<threshold
            RETURN p.id AS who,count(c) AS n,toString(threshold) AS label }
        RETURN threshold,who,n,label'''
    cases.append(('equal_numbers_different_types', typed, {}))
    global_count = '''UNWIND [{x:10},{x:0},{x:null}] AS record WITH record.x AS threshold
        CALL { WITH threshold MATCH(p:ProofPerson)-[:PROOF_LINK]->(c:ProofCity)
            WHERE c.id<threshold RETURN count(c) AS n } RETURN threshold,n'''
    cases.append(('inactive_global_aggregate', global_count, {}))
    report = dict(uri=uri, workload_queries_executed=False,
                  fixture='isolated unique labels, parallel edges, zero-count and null anchors',
                  transaction_rolled_back=False, results=[])
    try:
        with GraphDatabase.driver(uri, auth=None) as driver, driver.session() as session, \
                IndexMiddleware(Schema.from_text(names(theory_schema())),
                                profile=neo4j_profile(ROOT / 'profiles/hetionet-neo4j.yaml')) as mw:
            mw.register_neo4j_index(script)
            tx = session.begin_transaction()
            try:
                tx.run(names('''CREATE (a:ProofPerson {id:1}),(b:ProofPerson {id:2}),
                    (d:ProofPerson {id:3}),(x:ProofCity {id:5}),(y:ProofCity {id:15}),
                    (a)-[:PROOF_LINK]->(x),(a)-[:PROOF_LINK]->(x),
                    (a)-[:PROOF_LINK]->(y),(d)-[:PROOF_LINK]->(y)''')).consume()
                for statement in split_script(script):
                    tx.run(statement).consume()
                for label, source, params in cases:
                    source = names(source)
                    # Control: same engine/catalog and ordinary QueryPart
                    # rules, with ONLY the two new local rules disabled.
                    with patch('cico.scalar_subqueries.rewrite_scalar_regions', return_value=(None, [], [], [])), \
                            patch('cico.subqueries._finite_call', return_value=None):
                        before = mw.execute(source, parameters=params)
                    mw._cache.clear()
                    result = mw.execute(source, parameters=params)
                    row = dict(case=label, rewritten=result.rewritten, reason=result.reason,
                               before_rewritten=before.rewritten,
                               detail=result.detail, cypher=result.cypher, proofs=result.proofs)
                    report['results'].append(row)
                    if not result.rewritten:
                        row['equal'] = False
                        continue
                    tx.run('EXPLAIN ' + result.cypher, params).consume()
                    baseline = [r.data() for r in tx.run(source, params)]
                    rewritten = [r.data() for r in tx.run(result.cypher, params)]
                    canonical = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in rows)
                    row.update(equal=canonical(baseline) == canonical(rewritten),
                               baseline=baseline, result=rewritten)
            finally:
                tx.rollback()
                report['transaction_rolled_back'] = True
    finally:
        report['all_equal'] = bool(report['results']) and all(r.get('equal') for r in report['results'])
        report['before_rewritten'] = sum(r.get('before_rewritten', False) for r in report['results'])
        report['rewritten'] = sum(r.get('rewritten', False) for r in report['results'])
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, ensure_ascii=False))
    for row in report['results']:
        print(row['case'], row['rewritten'], row.get('equal'), row['detail'][:100])
    return 0 if report['all_equal'] and len(report['results']) == len(cases) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uri', default='bolt://127.0.0.1:7693')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.exit(run(args.uri, args.output))
