"""BI8 rewrite (constant key binding + correlated count) on real Neo4j, rolled back per seed.

The rewrite is specialized to $tag/$startDate/$endDate, so the FILL is
registered for a unique fixture tag name and a fixed window, the query is
rewritten once, and every seed builds a fixture in ONE transaction that is
rolled back: one Tag with that name (unique in the database), another Tag,
persons with interests, messages inside and outside the window, tagged with
either tag, and KNOWS edges.  bi8MessageCount is written on the fixture
Persons as the FILL defines it (0 where it emits no row).  The query starts
from the fixture tag name, so SF1 data is never matched.
"""
import argparse
import datetime
import json
import logging
import random
import sys
import uuid
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.middleware import inline_parameters
from cico.neo4j import neo4j_profile
from neug_fill_bi_status import statements

ROOT = Path(__file__).resolve().parents[1]
DOCS = Path('/mnt/sdb1/feiyang/IncGQ-experiment-docs')
UTC = datetime.timezone.utc
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)


def fixture(rng, tag, run):
    lines = [f'CREATE (t0:Tag {{name: "{tag}", fixture: "{run}"}}), (t1:Tag {{name: "{tag}_other", fixture: "{run}"}})']
    lines += [f'CREATE (p{x}:Person {{id: {-940000 - x}, fixture: "{run}"}})' for x in range(9)]
    lines += [f'CREATE (p{x})-[:HAS_INTEREST]->(t{rng.randrange(2)})' for x in range(9) if rng.random() < 0.35]
    for i in range(30):
        kind = 'Post' if rng.random() < 0.4 else 'Comment'
        day = rng.randrange(-5, 16)    # window is days 1..9 (exclusive ends)
        date = (datetime.datetime(2011, 7, 20, tzinfo=UTC) + datetime.timedelta(days=day, hours=rng.randrange(24)))
        lines.append(f'CREATE (m{i}:{kind}:Message {{id: {-940000 - i}, fixture: "{run}", '
                     f'creationDate: datetime("{date.strftime("%Y-%m-%dT%H:%M:%SZ")}")}})-[:HAS_CREATOR]->(p{rng.randrange(9)})')
        for t in range(2):
            if rng.random() < 0.6:
                lines.append(f'CREATE (m{i})-[:HAS_TAG]->(t{t})')
    lines += [f'CREATE (p{a})-[:KNOWS]->(p{b})' for a in range(9) for b in range(a + 1, 9) if rng.random() < 0.3]
    return '\n'.join(lines)


def run(uri, seeds, output):
    run_id = 'cico_proof8_' + uuid.uuid4().hex[:10]
    params = dict(tag=run_id, startDate=datetime.datetime(2011, 7, 20, tzinfo=UTC),
                  endDate=datetime.datetime(2011, 7, 29, tzinfo=UTC))
    query = (DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-8.cypher').read_text().replace('LIMIT 100', 'LIMIT 100000')
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        d = DOCS / 'NeuG/queries/index/ldbc-bi/bi8'
        for name in ('index.cypher', 'parameter.cypher'):
            text = (d / name).read_text()
            if name == 'parameter.cypher':
                text = inline_parameters(text, params)
            for s in statements(text):
                if not s.upper().startswith('MATCH'):
                    mw.register_index([s], input_dialect='neug') if s.upper().startswith('FILL') \
                        else mw.apply_ddl(s, input_dialect='neug')
        result = mw.execute(query, parameters=params)
        assert result.rewritten, result.to_dict()
    report = dict(uri=uri, workload_queries_executed=False, transaction_rolled_back=True,
                  rewritten=result.cypher, premises=result.premises, specialized=result.specialized, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver:
        for seed in range(seeds):
            rng = random.Random(seed)
            with driver.session() as session:
                tx = session.begin_transaction()
                try:
                    tx.run(fixture(rng, run_id, run_id))
                    tx.run('''MATCH (p:Person {fixture: $run})
                              SET p.bi8MessageCount = COUNT { MATCH (p)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag)
                                  WHERE t.name = $tag AND m.creationDate > $startDate AND m.creationDate < $endDate }''',
                           run=run_id, **params)
                    a = [r.values() for r in tx.run(query, **params)]
                    b = [r.values() for r in tx.run(result.cypher, **params)]
                    report['cases'].append(dict(seed=seed, rows=len(a), equal=a == b,
                                                nonzero=sum(1 for r in a if r[1] or r[2]), original=a[:4], rewritten=b[:4]))
                finally:
                    tx.rollback()
    report['all_equal'] = all(c['equal'] for c in report['cases'])
    report['rows_with_scores'] = sum(c['nonzero'] for c in report['cases'])
    Path(output).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ('all_equal', 'rows_with_scores')}), len(report['cases']))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--seeds', type=int, default=30)
    p.add_argument('--output', default='/tmp/bi8-fixture.json')
    a = p.parse_args()
    sys.exit(run(a.uri, a.seeds, a.output))
