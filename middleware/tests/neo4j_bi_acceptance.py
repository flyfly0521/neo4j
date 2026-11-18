"""Import-independent Neo4j BI acceptance: build indexes, rewrite, EXPLAIN only.

The workload queries are never executed. Requires a running private Neo4j and
an up-to-date experiment-docs checkout. --build executes only index setup SQL.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile
from cico.statements import split_script
import workloads as W


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--uri', default='bolt://127.0.0.1:7693')
    p.add_argument('--build', action='store_true')
    p.add_argument('--output', required=True)
    args = p.parse_args()
    from neo4j import GraphDatabase
    root = Path(__file__).resolve().parents[1]
    report = {'docs_commit': None, 'uri': args.uri, 'workload_queries_executed': False,
              'registration': [], 'build': [], 'queries': []}
    import subprocess
    report['docs_commit'] = subprocess.check_output(['git', '-C', str(W.DOCS), 'rev-parse', 'HEAD'], text=True).strip()
    with GraphDatabase.driver(args.uri, auth=None) as driver, driver.session() as session, IndexMiddleware(
            root / 'schemas/ldbc-bi.yaml', profile=neo4j_profile(root / 'profiles/ldbc-snb-neo4j.yaml')) as mw:
        driver.verify_connectivity()
        report['counts'] = session.run('MATCH (n) RETURN count(n) AS nodes').single().data()
        report['counts'].update(session.run('MATCH ()-[r]->() RETURN count(r) AS relationships').single().data())
        for number in (10, 9, 5, 6, 11):
            path = W.DOCS / f'Neo4j/queries/index/ldbc-bi/bi{number}/index.cypher'
            script = path.read_text()
            registered = mw.register_neo4j_index(script)
            report['registration'].append({'file': str(path), 'indexes': [r.to_dict() for r in registered]})
            if args.build:
                for i, statement in enumerate(split_script(script)):
                    session.run(statement).consume()
                    report['build'].append({'index': number, 'statement': i + 1, 'ok': True})
            print('registered', number, flush=True)
        report['generated_build'] = mw.build_statements()
        for path in W.query_files('ldbc-bi', 'neo4j'):
            name = W.query_name(path)
            text = Path(path).read_text()
            if W.is_write(text):
                report['queries'].append(dict(query=name, reason='write_query', rewritten=False))
                continue
            params = W.parameters('ldbc-bi', path, text)
            result = mw.execute(text, parameters=params)
            row = dict(query=name, **result.to_dict())
            if result.rewritten:
                used = {k: v.replace(tzinfo=datetime.timezone.utc) if isinstance(v, datetime.datetime) and v.tzinfo is None else v
                        for k, v in params.items() if '$' + k in result.cypher}
                try:
                    session.run('EXPLAIN ' + result.cypher, used).consume()
                    row['neo4j_explain'] = 'ok'
                except Exception as error:
                    row['neo4j_explain'] = str(error)
            report['queries'].append(row)
            print(name, result.reason, result.indexes, flush=True)
        report['rewritten'] = sum(q['rewritten'] for q in report['queries'])
        report['explain_ok'] = sum(q.get('neo4j_explain') == 'ok' for q in report['queries'])
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + '\n')
    assert report['rewritten'] > 0, 'No query was rewritten'
    assert report['rewritten'] == report['explain_ok'], 'Neo4j rejected rewritten SQL'


if __name__ == '__main__':
    main()
