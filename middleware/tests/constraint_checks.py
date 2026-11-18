"""Count violations of every declared profile constraint on a Neo4j database (read-only)."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile

ROOT = Path(__file__).resolve().parents[1]


def run(uri, output):
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        checks = mw.constraint_checks()
    results = []
    with GraphDatabase.driver(uri, auth=None) as driver, driver.session() as session:
        for check in checks:
            started = time.time()
            violations = session.run(check['cypher']).single()['violations']
            results.append(dict(check, violations=violations, seconds=round(time.time() - started, 2)))
            print(f"{violations:>8}  {check['constraint']}")
    report = dict(uri=uri, read_only=True, all_hold=all(r['violations'] == 0 for r in results), checks=results)
    if output:
        Path(output).write_text(json.dumps(report, indent=1))
    return 0 if report['all_hold'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--output')
    a = p.parse_args()
    sys.exit(run(a.uri, a.output))
