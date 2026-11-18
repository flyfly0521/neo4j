"""BI10 on the SF1 instance: original vs rewritten, read-only, a few parameter samples."""
import json
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import workloads as W
from neo4j import GraphDatabase
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile

ROOT = Path(__file__).resolve().parents[1]
FILL = (W.DOCS / 'NeuG/queries/index/ldbc-bi/bi10/index.cypher').read_text()


def main(uri, samples, output):
    path = W.DOCS / 'Neo4j/queries/baseline/ldbc-bi/bi-10.cypher'
    query = path.read_text()
    report = dict(uri=uri, read_only=True, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver, driver.session() as session:
        missing = session.run('MATCH (p:Person) WHERE p.countryName IS NULL OR p.countryName = "" '
                              'RETURN count(p) AS n').single()['n']
        stale = session.run('MATCH (p:Person)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(c:Country) '
                            'WHERE p.countryName <> c.name RETURN count(p) AS n').single()['n']
        report.update(persons_without_countryName=missing, persons_with_stale_countryName=stale)
        print('index state: missing', missing, 'stale', stale)
        with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
            from neug_fill_bi_status import statements
            for s in statements(FILL):
                mw.register_index([s], input_dialect='neug') if s.upper().startswith('FILL') \
                    else mw.apply_ddl(s, input_dialect='neug')
            for sample in range(samples):
                params = W.parameters('ldbc-bi', path, query, sample)
                r = mw.execute(query, parameters=params)
                case = dict(sample=sample, parameters={k: str(v) for k, v in params.items()}, rewritten=r.rewritten,
                            premises=r.premises, route=r.route)
                if r.rewritten:
                    times, outputs = [], []
                    for text in (query, r.cypher):
                        started = time.time()
                        outputs.append([rec.values() for rec in session.run(text, **params)])
                        times.append(round(time.time() - started, 2))
                    case.update(rows=len(outputs[0]), equal=outputs[0] == outputs[1], seconds=times)
                report['cases'].append(case)
                print(json.dumps(case)[:300], flush=True)
    report['all_equal'] = all(c.get('equal') for c in report['cases'])
    Path(output).write_text(json.dumps(report, indent=1, default=str))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'bolt://127.0.0.1:7693',
                  int(sys.argv[2]) if len(sys.argv) > 2 else 3,
                  sys.argv[3] if len(sys.argv) > 3 else '/tmp/bi10-sf1.json'))
