"""Relation-carrier path substitution (BI17) on real Neo4j, ONE ROLLED-BACK transaction per seed.

Isolation: the fixture's Tag has a unique name and the query starts from
that tag; every transaction is rolled back.  ROOT_POST is written on the
fixture exactly as its FILL defines it:
    (m)-[:ROOT_POST]->(p)  for every m -[:REPLY_OF*0..]-> p:Post
Random forests of posts and replies, memberships, tags and timestamps cover
zero-length paths (message2 = post2), shared trees (forum1 = forum2, which
the query filters) and repeated authors.  Does not execute BI workloads on
SF1 data.
"""
import argparse
import datetime
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
DOCS = Path('/mnt/sdb1/feiyang/IncGQ-experiment-docs')
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
BASE = datetime.datetime(2012, 1, 1, tzinfo=datetime.timezone.utc)


def fixture(rng):
    persons, forums = list(range(7)), list(range(3))
    members = [(f, p) for f in forums for p in persons if rng.random() < 0.6]
    messages = []      # (id, kind, creator, forum, parent or None, hours)
    for i in range(24):
        if i < 6 or rng.random() < 0.2:
            messages.append((i, 'Post', rng.choice(persons), rng.choice(forums), None, rng.randrange(48)))
        else:
            parent = rng.choice(messages)
            messages.append((i, 'Comment', rng.choice(persons), parent[3], parent[0],
                             parent[5] + rng.randrange(12)))
    tagged = [(m[0], 0) for m in messages if rng.random() < 0.7] + \
             [(m[0], 1) for m in messages if rng.random() < 0.3]
    return dict(persons=persons, forums=forums, members=members, messages=messages, tagged=tagged)


def load(tx, f, tag):
    """One statement: every fixture node is bound to a variable, so no lookup
    ever scans the SF1 labels (ids are fixture-local and never matched)."""
    k = lambda kind, i: f'{kind}{i}'
    lines = [f'CREATE (tag0:Tag {{name: "{tag}_0", fixture: "{tag}"}}), (tag1:Tag {{name: "{tag}_1", fixture: "{tag}"}})']
    lines += [f'CREATE ({k("p", p)}:Person {{id: {-910000 - p}, fixture: "{tag}"}})' for p in f['persons']]
    lines += [f'CREATE ({k("f", x)}:Forum {{id: {-910000 - x}, fixture: "{tag}"}})' for x in f['forums']]
    lines += [f'CREATE ({k("f", fo)})-[:HAS_MEMBER]->({k("p", p)})' for fo, p in f['members']]
    for i, kind, creator, forum, parent, hours in f['messages']:
        date = (BASE + datetime.timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
        lines.append(f'CREATE ({k("m", i)}:{kind}:Message {{id: {-910000 - i}, fixture: "{tag}", '
                     f'creationDate: datetime("{date}")}})-[:HAS_CREATOR]->({k("p", creator)})')
        lines.append(f'CREATE ({k("f", forum)})-[:CONTAINER_OF]->({k("m", i)})' if parent is None
                     else f'CREATE ({k("m", i)})-[:REPLY_OF]->({k("m", parent)})')
    lines += [f'CREATE ({k("m", i)})-[:HAS_TAG]->(tag{t})' for i, t in f['tagged']]
    tx.run('\n'.join(lines))
    # ROOT_POST exactly as its FILL defines it, over fixture messages only
    tx.run('''MATCH (m:Message {fixture: $tag})-[:REPLY_OF*0..]->(p:Post)
              CREATE (m)-[:ROOT_POST]->(p)''', tag=tag)


def rows(tx, query, params):
    return Counter(tuple(sorted(r.items())) for r in (rec.data() for rec in tx.run(query, **params)))


def run(uri, seeds, output):
    statements = [s.strip() for s in (DOCS / 'NeuG/queries/index/ldbc-bi/bi17/index.cypher').read_text().split(';')
                  if s.strip()]
    query = (DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-17.cypher').read_text().replace('LIMIT 10', 'LIMIT 100000')
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        for s in statements:
            mw.execute(s) if not s.upper().startswith('FILL') else mw.register_index([s], input_dialect='neug')
        result = mw.execute(query, parameters={'tag': 'x', 'delta': 4})
        assert result.rewritten, result.to_dict()
        rewritten = result.cypher
    report = dict(uri=uri, workload_queries_executed=False, transaction_rolled_back=True,
                  rewritten=rewritten, proofs=result.proofs, premises=result.premises, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver:
        for seed in range(seeds):
            rng = random.Random(seed)
            tag = 'cico_proof17_' + uuid.uuid4().hex[:10]
            f = fixture(rng)
            with driver.session() as session:
                tx = session.begin_transaction()
                try:
                    load(tx, f, tag)
                    for delta in (0, 4, 24):
                        params = {'tag': tag + '_0', 'delta': delta}
                        a, b = rows(tx, query, params), rows(tx, rewritten, params)
                        case = dict(seed=seed, delta=delta, rows=sum(a.values()), equal=a == b)
                        if a != b:
                            case.update(original_only=str(a - b)[:400], rewritten_only=str(b - a)[:400])
                        report['cases'].append(case)
                finally:
                    tx.rollback()
    report['all_equal'] = all(c['equal'] for c in report['cases'])
    report['nonempty'] = sum(1 for c in report['cases'] if c['rows'])
    Path(output).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ('all_equal', 'nonempty')}), len(report['cases']))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--seeds', type=int, default=20)
    p.add_argument('--output', default='/tmp/path-carrier.json')
    a = p.parse_args()
    sys.exit(run(a.uri, a.seeds, a.output))
