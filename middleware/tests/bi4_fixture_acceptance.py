"""BI4 rewrite (DISTINCT partition carrier) on real Neo4j, ONE ROLLED-BACK transaction per seed.

The top-forum selection in front of the CALL ranges over the whole SF1
database, so both the original and the rewritten statement get the same
fixture prefix instead: the list of fixture forums flagged `top`, with its
first element repeated (the rewrite must not double count duplicates).
Random forums, posts, reply forests, authors and memberships; one forum is
outside the list.  BI4_MESSAGE_COUNT is written exactly as its FILL defines
it.  Every transaction is rolled back; SF1 data is never matched.
"""
import argparse
import json
import logging
import random
import sys
import uuid
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile
from neug_fill_bi_status import statements
import workloads as W

ROOT = Path(__file__).resolve().parents[1]
DOCS = Path('/mnt/sdb1/feiyang/IncGQ-experiment-docs')
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
PREFIX = ('MATCH (f:Forum {fixture: $tag, top: true}) WITH f ORDER BY f.id '
          'WITH collect(f) AS fs WITH fs + fs[0..1] AS topForums\n')


def fixture(rng, tag):
    lines = [f'CREATE (f{x}:Forum {{id: {-930000 - x}, fixture: "{tag}", top: {str(x < 3).lower()}}})'
             for x in range(4)]
    lines += [f'CREATE (p{x}:Person {{id: {-930000 - x}, fixture: "{tag}"}})' for x in range(8)]
    lines += [f'CREATE (f{x})-[:HAS_MEMBER]->(p{y})' for x in range(4) for y in range(8) if rng.random() < 0.3]
    messages = []
    for i in range(30):
        if i < 6 or rng.random() < 0.15:
            forum = rng.randrange(4)
            lines.append(f'CREATE (m{i}:Post:Message {{id: {-930000 - i}, fixture: "{tag}"}})'
                         f'-[:HAS_CREATOR]->(p{rng.randrange(8)})')
            lines.append(f'CREATE (f{forum})-[:CONTAINER_OF]->(m{i})')
        else:
            parent = rng.choice(messages)
            lines.append(f'CREATE (m{i}:Comment:Message {{id: {-930000 - i}, fixture: "{tag}"}})'
                         f'-[:HAS_CREATOR]->(p{rng.randrange(8)})')
            lines.append(f'CREATE (m{i})-[:REPLY_OF]->(m{parent})')
        messages.append(i)
    return '\n'.join(lines)


def run(uri, seeds, output):
    query = (DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-4.cypher').read_text()
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        for s in statements((DOCS / 'NeuG/queries/index/ldbc-bi/bi4/index.cypher').read_text()):
            mw.register_index([s], input_dialect='neug') if s.upper().startswith('FILL') \
                else mw.apply_ddl(s, input_dialect='neug')
        path = DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-4.cypher'
        result = mw.execute(query, parameters=W.parameters('ldbc-bi', path, query))
        assert result.rewritten, result.to_dict()
    def fixture_text(text):
        call = text.index('CALL {')
        return (PREFIX + text[call:]).replace('LIMIT 100', 'LIMIT 100000')
    original, rewritten = fixture_text(query), fixture_text(result.cypher)
    report = dict(uri=uri, workload_queries_executed=False, transaction_rolled_back=True,
                  original=original, rewritten=rewritten, proofs=result.proofs, premises=result.premises, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver:
        for seed in range(seeds):
            rng = random.Random(seed)
            tag = 'cico_proof4_' + uuid.uuid4().hex[:10]
            with driver.session() as session:
                tx = session.begin_transaction()
                try:
                    tx.run(fixture(rng, tag))
                    tx.run('''MATCH (forum:Forum {fixture: $tag})-[:CONTAINER_OF]->(post:Post)
                                    <-[:REPLY_OF*0..]-(m:Message)-[:HAS_CREATOR]->(p:Person)
                              WITH forum, p, count(DISTINCT m) AS n
                              CREATE (forum)-[:BI4_MESSAGE_COUNT {messageCount: n}]->(p)''', tag=tag)
                    a = [r.values() for r in tx.run(original, tag=tag)]
                    b = [r.values() for r in tx.run(rewritten, tag=tag)]
                    report['cases'].append(dict(seed=seed, rows=len(a), nonzero=sum(1 for r in a if r[-1]),
                                                equal=a == b, original=a[:5], rewritten=b[:5]))
                finally:
                    tx.rollback()
    report['all_equal'] = all(c['equal'] for c in report['cases'])
    report['rows_with_messages'] = sum(c['nonzero'] for c in report['cases'])
    Path(output).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ('all_equal', 'rows_with_messages')}), len(report['cases']))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--seeds', type=int, default=30)
    p.add_argument('--output', default='/tmp/bi4-fixture.json')
    a = p.parse_args()
    sys.exit(run(a.uri, a.seeds, a.output))
