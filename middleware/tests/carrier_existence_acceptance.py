"""Relationship-carrier existence rule on real Neo4j, ONE ROLLED-BACK transaction per seed.

The fixture lives in the SF1 database but is isolated: its Countries carry
unique names, so the queries (anchored on Country names) see only fixture
nodes, and every transaction is rolled back.  Carrier values are written on
the fixture KNOWS edges exactly as the FILL defines them:
    X(e: u->v) = (#KNOWS u->v) * |W_P'(pA:=u, pB:=v)|
Random multigraphs cover both KNOWS directions, parallel KNOWS edges, pairs
without KNOWS (fallback branch), comments replying to comments and posts,
and likes in both directions.  Does not execute BI workloads on SF1 data.
"""
import argparse
import json
import logging
import random
import sys
import uuid
from collections import Counter
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile

ROOT = Path(__file__).resolve().parents[1]
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)

FILLS = {  # carrier column -> P'(pA, pB) in Neo4j spelling (from the NeuG FILL file)
    'bi14_case1_fwd': '(pA)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(pB)',
    'bi14_case1_rev': '(pB)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(pA)',
    'bi14_case2_fwd': '(pA)<-[:HAS_CREATOR]-(m:Message)<-[:REPLY_OF]-(:Comment)-[:HAS_CREATOR]->(pB)',
    'bi14_case2_rev': '(pB)<-[:HAS_CREATOR]-(m:Message)<-[:REPLY_OF]-(:Comment)-[:HAS_CREATOR]->(pA)',
    'bi14_case3_fwd': '(pA)-[:LIKES]->(m:Message)-[:HAS_CREATOR]->(pB)',
    'bi14_case3_rev': '(pB)-[:LIKES]->(m:Message)-[:HAS_CREATOR]->(pA)',
    'bi14_case4_fwd': '(pA)<-[:HAS_CREATOR]-(m:Message)<-[:LIKES]-(pB)',
    'bi14_case4_rev': '(pB)<-[:HAS_CREATOR]-(m:Message)<-[:LIKES]-(pA)',
}
DOCS = Path('/mnt/sdb1/feiyang/IncGQ-experiment-docs')
NO_KNOWS = '''MATCH
  (country1:Country {name: $country1})<-[:IS_PART_OF]-(city1:City)<-[:IS_LOCATED_IN]-(person1:Person),
  (country2:Country {name: $country2})<-[:IS_PART_OF]-(city2:City)<-[:IS_LOCATED_IN]-(person2:Person)
WITH person1, person2, city1, 0 AS score
OPTIONAL MATCH (person1)<-[:HAS_CREATOR]-(c:Comment)-[:REPLY_OF]->(:Message)-[:HAS_CREATOR]->(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN c IS NULL THEN 0 ELSE 4 END) AS score
OPTIONAL MATCH (person1)-[:LIKES]->(m:Message)-[:HAS_CREATOR]->(person2)
WITH DISTINCT person1, person2, city1, score + (CASE WHEN m IS NOT NULL THEN 10 ELSE 0 END) AS score
RETURN person1.id AS p1, person2.id AS p2, city1.name AS city, score'''


def fixture(rng, tag):
    persons = list(range(8))
    side = {p: 0 if p < 4 else 1 for p in persons}
    city = {p: (side[p], rng.randrange(2)) for p in persons}
    knows = []
    for a in range(4):
        for b in range(4, 8):
            roll = rng.random()
            if roll < 0.25:
                continue                                  # no KNOWS: fallback branch
            knows.append((a, b) if roll < 0.55 else (b, a))
            if roll > 0.9:
                knows.append(rng.choice([(a, b), (b, a)]))  # parallel or opposite edge
    messages, replies, likes = [], [], []
    for i in range(18):
        kind = 'Post' if i < 6 else 'Comment'
        messages.append((i, kind, rng.choice(persons)))
        if kind == 'Comment':
            replies.append((i, rng.randrange(i)))          # acyclic reply forest
    for _ in range(10):
        likes.append((rng.choice(persons), rng.randrange(18)))
    return dict(tag=tag, persons=persons, city=city, knows=knows, messages=messages, replies=replies, likes=likes)


def load(tx, f):
    tag = f['tag']
    tx.run('UNWIND [0,1] AS s CREATE (:Country {name: $tag + "_" + s, fixture: $tag})', tag=tag)
    tx.run('''UNWIND [[0,0],[0,1],[1,0],[1,1]] AS sc
              MATCH (n:Country {name: $tag + "_" + sc[0]})
              CREATE (:City {name: $tag + "_city_" + sc[0] + "_" + sc[1], fixture: $tag})-[:IS_PART_OF]->(n)''', tag=tag)
    tx.run('''UNWIND $rows AS r
              MATCH (c:City {name: $tag + "_city_" + r.s + "_" + r.c})
              CREATE (:Person {id: -900000 - r.p, fixture: $tag})-[:IS_LOCATED_IN]->(c)''',
           tag=tag, rows=[dict(p=p, s=f['city'][p][0], c=f['city'][p][1]) for p in f['persons']])
    tx.run('''UNWIND $rows AS r MATCH (a:Person {id: -900000 - r[0], fixture: $tag}),
              (b:Person {id: -900000 - r[1], fixture: $tag}) CREATE (a)-[:KNOWS]->(b)''', tag=tag, rows=f['knows'])
    for kind in ('Post', 'Comment'):
        tx.run(f'''UNWIND $rows AS r MATCH (p:Person {{id: -900000 - r[1], fixture: $tag}})
                  CREATE (:{kind}:Message {{id: -900000 - r[0], fixture: $tag}})-[:HAS_CREATOR]->(p)''',
               tag=tag, rows=[(i, p) for i, k, p in f['messages'] if k == kind])
    tx.run('''UNWIND $rows AS r MATCH (c:Comment {id: -900000 - r[0], fixture: $tag}),
              (m:Message {id: -900000 - r[1], fixture: $tag}) CREATE (c)-[:REPLY_OF]->(m)''', tag=tag, rows=f['replies'])
    tx.run('''UNWIND $rows AS r MATCH (p:Person {id: -900000 - r[0], fixture: $tag}),
              (m:Message {id: -900000 - r[1], fixture: $tag}) CREATE (p)-[:LIKES]->(m)''', tag=tag, rows=f['likes'])
    for column, pattern in FILLS.items():
        tx.run(f'''MATCH (pA:Person {{fixture: $tag}})-[k:KNOWS]->(pB:Person)
                   SET k.{column} = COUNT {{ MATCH (pA)-[:KNOWS]->(pB) }} * COUNT {{ MATCH {pattern} }}''', tag=tag)


def rows(tx, query, params):
    return Counter(tuple(sorted(r.items())) for r in (rec.data() for rec in tx.run(query, **params)))


def run(uri, seeds, output):
    text = (DOCS / 'NeuG/queries/index/ldbc-bi/bi14/index.cypher').read_text()
    statements = [s.strip() for s in text.split(';') if s.strip()]
    bi14 = (DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-14.cypher').read_text()
    bi14 = bi14.replace('LIMIT 100', 'LIMIT 100000')  # compare complete results, not a tie-broken prefix
    report = dict(uri=uri, workload_queries_executed=False, transaction_rolled_back=True, cases=[])
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw, \
            GraphDatabase.driver(uri, auth=None) as driver:
        for s in statements:
            if s.upper().startswith('ALTER'):
                mw.execute(s)
        for s in statements:
            if s.upper().startswith('FILL'):
                mw.register_index([s], input_dialect='neug')
        rewritten = {}
        for name, query in (('bi14', bi14), ('no_knows_fallback', NO_KNOWS)):
            result = mw.execute(query, parameters={'country1': 'x', 'country2': 'y'})
            assert result.rewritten, (name, result.to_dict())
            rewritten[name] = (query, result.cypher, result.proofs)
        for seed in range(seeds):
            rng = random.Random(seed)
            tag = 'cico_proof_' + uuid.uuid4().hex[:10]
            f = fixture(rng, tag)
            with driver.session() as session:
                tx = session.begin_transaction()
                try:
                    load(tx, f)
                    params = {'country1': tag + '_0', 'country2': tag + '_1'}
                    for name, (original, output_text, _) in rewritten.items():
                        a, b = rows(tx, original, params), rows(tx, output_text, params)
                        report['cases'].append(dict(seed=seed, query=name, rows=sum(a.values()),
                                                    knows=len(f['knows']), equal=a == b))
                        if a != b:
                            report['cases'][-1]['difference'] = dict(original_only=str(a - b)[:500],
                                                                     rewritten_only=str(b - a)[:500])
                finally:
                    tx.rollback()
    report['rewrites'] = {k: dict(cypher=v[1], proofs=v[2]) for k, v in rewritten.items()}
    report['all_equal'] = all(c['equal'] for c in report['cases'])
    report['nonempty'] = sum(1 for c in report['cases'] if c['rows'])
    Path(output).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ('all_equal', 'nonempty')}), len(report['cases']))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--seeds', type=int, default=30)
    p.add_argument('--output', default='/tmp/carrier-existence.json')
    a = p.parse_args()
    sys.exit(run(a.uri, a.seeds, a.output))
