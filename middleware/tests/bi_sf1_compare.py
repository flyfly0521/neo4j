"""BI-N on the SF1 instance: original vs rewritten, read-only, a few parameter samples.

Usage: bi_sf1_compare.py N INDEX_DIR [URI] [SAMPLES] [OUTPUT]; the index must already be built."""
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


def main(number, index_dir, uri, samples, output):
    FILL = '\n;\n'.join(p.read_text() for p in sorted((W.DOCS / 'NeuG/queries/index/ldbc-bi' / index_dir).glob('index.cypher')))
    path = W.DOCS / f'Neo4j/queries/baseline/ldbc-bi/bi-{number}.cypher'
    query = path.read_text()
    report = dict(uri=uri, read_only=True, cases=[])
    with GraphDatabase.driver(uri, auth=None) as driver, driver.session() as session:
        if index_dir in ('bi10', 'bi11'):
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
                    case.update(rows=len(outputs[0]), equal=outputs[0] == outputs[1], seconds=times,
                                first_row=outputs[0][0] if outputs[0] else None)
                report['cases'].append(case)
                print(json.dumps(case)[:300], flush=True)
    report['all_equal'] = all(c.get('equal') for c in report['cases'])
    Path(output).write_text(json.dumps(report, indent=1, default=str))
    return 0 if report['all_equal'] else 1


if __name__ == '__main__':
    a = sys.argv[1:]
    sys.exit(main(int(a[0]), a[1], a[2] if len(a) > 2 else 'bolt://127.0.0.1:7693',
                  int(a[3]) if len(a) > 3 else 3, a[4] if len(a) > 4 else f'/tmp/bi{a[0]}-sf1.json'))
