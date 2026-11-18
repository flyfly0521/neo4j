"""End-to-end check on a real NeuG database.

1. Build a small random graph and materialize FILL indexes with NeuG itself.
2. For each query: run the original with index rewriting disabled (ground
   truth), ask the middleware for a rewrite, run the printed Cypher with
   rewriting disabled (so it executes exactly as printed), compare results.

  python3 tests/e2e_neug.py [--workdir /tmp/cico_e2e] [--seed 7]
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cico import IndexMiddleware  # noqa: E402
from cico.engine import resolve_binary  # noqa: E402

EXEC = resolve_binary('cico_exec', 'CICO_EXEC')

DDL = [
    'CREATE NODE TABLE Person(id INT64, name STRING, age INT64, PRIMARY KEY(id))',
    'CREATE NODE TABLE City(id INT64, name STRING, PRIMARY KEY(id))',
    'CREATE NODE TABLE Country(id INT64, name STRING, PRIMARY KEY(id))',
    'CREATE NODE TABLE Restaurant(id INT64, stars INT64, PRIMARY KEY(id))',
    'CREATE REL TABLE LIVES_IN(FROM Person TO City)',
    'CREATE REL TABLE HOME(FROM Person TO City, MANY_ONE)',
    'CREATE REL TABLE IN_COUNTRY(FROM City TO Country, MANY_ONE)',
    'CREATE REL TABLE HAS_RESTAURANT(FROM City TO Restaurant)',
    'CREATE REL TABLE LIKES(FROM Person TO Restaurant)',
    'CREATE REL TABLE KNOWS(FROM Person TO Person)',
]

FILLS = {
    'person_restaurants': 'FILL Person(iv_rest) FROM (MATCH (a:Person)-[:LIVES_IN]->(b:City)'
                          '-[:HAS_RESTAURANT]->(c:Restaurant) RETURN a.id, count(*))',
    'person_country': 'FILL Person(countryName) FROM (MATCH (p:Person)-[:HOME]->(c:City)'
                      '-[:IN_COUNTRY]->(k:Country) RETURN p.id, k.name)',
    'city_likes': 'FILL City(iv_likes) FROM (MATCH (c:City)-[:HAS_RESTAURANT]->(r:Restaurant)'
                  '<-[:LIKES]-(p:Person) RETURN c.id, count(*))',
    'knows_common': 'FILL KNOWS(iv_common) FROM (MATCH (a:Person)-[e:KNOWS]->(b:Person) WITH a, b '
                    'MATCH (a)-[:LIKES]->(r:Restaurant)<-[:LIKES]-(b) RETURN a.id, b.id, count(*))',
}

# Physical carrier columns must exist before FILL (milestone M2 will derive these).
CARRIERS = {
    'person_restaurants': ['ALTER TABLE Person ADD iv_rest INT64'],
    'person_country': ['ALTER TABLE Person ADD countryName STRING'],
    'city_likes': ['ALTER TABLE City ADD iv_likes INT64'],
    'knows_common': ['ALTER TABLE KNOWS ADD iv_common INT64'],
}

QUERIES = [
    # NODE COUNT index
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) RETURN x.id AS person, count(*) AS n',
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) WHERE x.age > 30 '
    'RETURN x.id AS person, x.name AS name, count(*) AS n ORDER BY n DESC, person LIMIT 10',
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) RETURN count(*) AS total',
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) WITH x, count(*) AS n '
    'WHERE n > 3 RETURN x.id AS id, n',
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) WHERE x.id < $limit '
    'RETURN x.id AS person, count(*) AS n',
    # NODE pushdown along an n:1 chain
    'MATCH (p:Person)-[:HOME]->(c:City)-[:IN_COUNTRY]->(k:Country) RETURN p.id AS id, k.name AS country',
    'MATCH (p:Person)-[:HOME]->(c:City)-[:IN_COUNTRY]->(k:Country) WHERE k.name = \'country_1\' RETURN count(*) AS n',
    # second NODE COUNT index, reverse direction in the query
    'MATCH (c:City)-[:HAS_RESTAURANT]->(r:Restaurant)<-[:LIKES]-(p:Person) RETURN c.name AS city, count(*) AS likes',
    'MATCH (p:Person)-[:LIKES]->(r:Restaurant)<-[:HAS_RESTAURANT]-(c:City) RETURN c.id AS city, count(*) AS likes',
    # REL aggregate index
    'MATCH (a:Person)-[e:KNOWS]->(b:Person) MATCH (a)-[:LIKES]->(r:Restaurant)<-[:LIKES]-(b) '
    'RETURN a.id AS src, b.id AS dst, count(*) AS common',
    # combination and non-applicable controls
    'MATCH (x:Person)-[:LIVES_IN]->(y:City)-[:HAS_RESTAURANT]->(z:Restaurant) WHERE z.stars >= 3 RETURN x.id AS person, count(*) AS n',
    'MATCH (x:Person) RETURN x.id AS id, x.age AS age ORDER BY age DESC, id LIMIT 5',
]
PARAMS = {'limit': 50}


class Exec:
    def __init__(self, db):
        self.p = subprocess.Popen([str(EXEC), '--db', str(db)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        ready = json.loads(self.p.stdout.readline())
        if not ready.get('ok'):
            raise RuntimeError(ready)

    def run(self, cypher, mode='read', params=None, disable_rewrite=True):
        self.p.stdin.write(json.dumps(dict(cypher=cypher, mode=mode, params=params or {},
                                           disable_rewrite=disable_rewrite)) + '\n')
        self.p.stdin.flush()
        response = json.loads(self.p.stdout.readline())
        if not response['ok']:
            raise RuntimeError(f"{response['error']}\n  in: {cypher}")
        return response['rows']

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=600)


def build(db, seed):
    rng = random.Random(seed)
    ex = Exec(db)
    for ddl in DDL:
        ex.run(ddl, 'schema')
    persons, cities, countries, restaurants = 120, 15, 4, 60
    for i in range(countries):
        ex.run(f"CREATE (:Country {{id: {i}, name: 'country_{i}'}})", 'insert')
    for i in range(cities):
        ex.run(f"CREATE (:City {{id: {i}, name: 'city_{i}'}})", 'insert')
        ex.run(f'MATCH (c:City {{id: {i}}}), (k:Country {{id: {rng.randrange(countries)}}}) '
               'CREATE (c)-[:IN_COUNTRY]->(k)', 'insert')
    for i in range(restaurants):
        ex.run(f'CREATE (:Restaurant {{id: {i}, stars: {rng.randint(1, 5)}}})', 'insert')
        for c in rng.sample(range(cities), rng.choice([0, 1, 1, 2])):
            ex.run(f'MATCH (c:City {{id: {c}}}), (r:Restaurant {{id: {i}}}) CREATE (c)-[:HAS_RESTAURANT]->(r)', 'insert')
    for i in range(persons):
        ex.run(f"CREATE (:Person {{id: {i}, name: 'p{i}', age: {rng.randint(18, 70)}}})", 'insert')
    for i in range(persons):
        if rng.random() < 0.9:  # some persons have no home -> pushdown must handle absence
            ex.run(f'MATCH (p:Person {{id: {i}}}), (c:City {{id: {rng.randrange(cities)}}}) CREATE (p)-[:HOME]->(c)', 'insert')
        for c in rng.sample(range(cities), rng.choice([0, 1, 2, 3])):
            ex.run(f'MATCH (p:Person {{id: {i}}}), (c:City {{id: {c}}}) CREATE (p)-[:LIVES_IN]->(c)', 'insert')
        for r in rng.sample(range(restaurants), rng.randint(0, 6)):
            ex.run(f'MATCH (p:Person {{id: {i}}}), (r:Restaurant {{id: {r}}}) CREATE (p)-[:LIKES]->(r)', 'insert')
        for j in rng.sample(range(persons), rng.randint(0, 5)):
            if j != i:
                ex.run(f'MATCH (a:Person {{id: {i}}}), (b:Person {{id: {j}}}) CREATE (a)-[:KNOWS]->(b)', 'insert')
    built = {}
    for name, fill in FILLS.items():
        try:
            for ddl in CARRIERS[name]:
                ex.run(ddl, 'schema')
            ex.run(fill, 'schema')
            built[name] = fill
        except RuntimeError as error:
            print(f'FILL {name} failed in NeuG, skipped: {error}', file=sys.stderr)
    ex.close()
    return built


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', default='/tmp/cico_e2e')
    parser.add_argument('--seed', type=int, default=7)
    args = parser.parse_args()
    work = Path(args.workdir)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    db = work / 'db'
    built = build(db, args.seed)
    schema = db / 'graph.yaml'
    if not schema.is_file():
        raise SystemExit(f'{schema} missing after close; inspect {db}')
    # NeuG caches compiled plans by query text, so ground truth (rewriting
    # disabled) and NeuG's own rewrite must run in separate processes; a
    # database can be opened by one process at a time, so run them in turn.
    engine_ex = Exec(db)
    engine_rows = {}
    for query in QUERIES:
        params = {k: v for k, v in PARAMS.items() if '$' + k in query}
        engine_rows[query] = sorted(engine_ex.run(query, params=params, disable_rewrite=False))
    engine_ex.close()
    ex = Exec(db)
    failures = 0
    with IndexMiddleware(schema, engine_log=work / 'engine.log') as rewriter:
        for name, fill in built.items():
            registered = rewriter.execute(fill, index_id=name)
            if not registered.ok:
                print('index not registered:', name, registered.detail)
        for query in QUERIES:
            params = {k: v for k, v in PARAMS.items() if '$' + k in query}
            truth = sorted(ex.run(query, params=params))
            engine = engine_rows[query]
            result = rewriter.rewrite(query, parameters=params)
            try:
                got = sorted(ex.run(result.cypher, params=params))
                status = 'OK' if got == truth else 'WRONG'
            except RuntimeError as error:
                status, got = 'EXEC_ERROR', str(error)
            engine_status = 'neug_inengine_ok' if engine == truth else 'NEUG_INENGINE_ALSO_WRONG'
            failures += status != 'OK'
            if status == 'WRONG' and got == engine:
                status = 'WRONG(same as NeuG in-engine rewrite)'
            print(f'[{status}] [{result.reason}{" " + str(result.indexes) if result.indexes else ""}] '
                  f'[{engine_status}] rows={len(truth)}\n  in : {query}\n  out: {result.cypher}')
            if result.detail and not result.rewritten:
                print(f'  why: {result.detail[:300]}')
            if status != 'OK':
                print(f'  expected {truth[:5]}\n  got      {got if isinstance(got, str) else got[:5]}')
    ex.close()
    print(f'\n{len(QUERIES) - failures}/{len(QUERIES)} queries return identical results')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
