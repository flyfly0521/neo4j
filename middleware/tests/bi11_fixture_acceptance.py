"""BI11 rewrite (key identities + countryName) on real Neo4j, ONE ROLLED-BACK transaction per seed.

SF1 parameter samples give count 0 for BI11, which proves nothing, so this
uses dense random fixtures: two Countries with unique fixture names, two
Cities each, ten Persons, random KNOWS edges with creation dates.  The
fixture satisfies the declared constraints (every Person in one City, every
City in one Country, unique Country names).  countryName is written on the
fixture Persons exactly as the FILL defines it.  Queries start from the
fixture Country name, so SF1 data is never matched; every transaction is
rolled back.
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
from cico.neo4j import neo4j_profile
from neug_fill_bi_status import statements

ROOT = Path(__file__).resolve().parents[1]
DOCS = Path('/mnt/sdb1/feiyang/IncGQ-experiment-docs')
logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)


def fixture(rng, tag):
    lines = [f'CREATE (c{c}:Country {{name: "{tag}_{c}", fixture: "{tag}"}})' for c in range(2)]
    lines += [f'CREATE (t{c}{k}:City {{name: "{tag}_city{c}{k}", fixture: "{tag}"}})-[:IS_PART_OF]->(c{c})'
              for c in range(2) for k in range(2)]
    for p in range(10):
        c, k = rng.randrange(2) if p >= 2 else 0, rng.randrange(2)
        lines.append(f'CREATE (p{p}:Person {{id: {-920000 - p}, fixture: "{tag}"}})-[:IS_LOCATED_IN]->(t{c}{k})')
    for a in range(10):
        for b in range(a + 1, 10):
            if rng.random() < 0.45:
                day = datetime.date(2012, 1, 1) + datetime.timedelta(days=rng.randrange(365))
                x, y = (a, b) if rng.random() < 0.5 else (b, a)
                lines.append(f'CREATE (p{x})-[:KNOWS {{creationDate: datetime("{day}T00:00:00Z")}}]->(p{y})')
    return '\n'.join(lines)


def run(uri, seeds, output):
    query = (DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-11.cypher').read_text()
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        for s in statements((DOCS / 'NeuG/queries/index/ldbc-bi/bi11/index.cypher').read_text()):
            mw.register_index([s], input_dialect='neug') if s.upper().startswith('FILL') \
                else mw.apply_ddl(s, input_dialect='neug')
        sample = {'country': 'x', 'startDate': datetime.datetime(2012, 1, 1, tzinfo=datetime.timezone.utc),
                  'endDate': datetime.datetime(2013, 1, 1, tzinfo=datetime.timezone.utc)}
        result = mw.execute(query, parameters=sample)
        assert result.rewritten, result.to_dict()
    report = dict(uri=uri, workload_queries_executed=False, transaction_rolled_back=True,
                  rewritten=result.cypher, premises=result.premises, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver:
        for seed in range(seeds):
            rng = random.Random(seed)
            tag = 'cico_proof11_' + uuid.uuid4().hex[:10]
            with driver.session() as session:
                tx = session.begin_transaction()
                try:
                    tx.run(fixture(rng, tag))
                    tx.run('''MATCH (p:Person {fixture: $tag})-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(c:Country)
                              SET p.countryName = c.name''', tag=tag)
                    for country in (tag + '_0', tag + '_1'):
                        for start, end in (((2012, 1, 1), (2013, 1, 1)), ((2012, 3, 1), (2012, 9, 1))):
                            params = dict(country=country,
                                          startDate=datetime.datetime(*start, tzinfo=datetime.timezone.utc),
                                          endDate=datetime.datetime(*end, tzinfo=datetime.timezone.utc))
                            a = [r.values() for r in tx.run(query, **params)]
                            b = [r.values() for r in tx.run(result.cypher, **params)]
                            report['cases'].append(dict(seed=seed, country=country[-1], window=[start, end],
                                                        original=a, rewritten=b, equal=a == b))
                finally:
                    tx.rollback()
    report['all_equal'] = all(c['equal'] for c in report['cases'])
    report['nonzero'] = sum(1 for c in report['cases'] if c['original'] and c['original'][0][0])
    Path(output).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ('all_equal', 'nonzero')}), len(report['cases']))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--seeds', type=int, default=20)
    p.add_argument('--output', default='/tmp/bi11-fixture.json')
    a = p.parse_args()
    sys.exit(run(a.uri, a.seeds, a.output))
