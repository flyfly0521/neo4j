"""Audit all 20 original Neo4j BI queries, with EXPLAIN and no execution.

Parameterized indexes are registered per sample in isolated metadata catalogs;
their physical properties are not rebuilt by this compile-only audit.
Exit 1 means coverage is incomplete; exit 2 means an invalid rewrite was found.
"""
import argparse
import collections
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from cico import IndexMiddleware
from cico.neo4j import neo4j_profile
import workloads as W

ROOT = Path(__file__).resolve().parents[1]
STATIC = (10, 9, 5, 6, 11)
PARAMETERIZED = {2, 8, 13, 16}


def inventory():
    out = []
    index_root = W.DOCS / 'Neo4j/queries/index/ldbc-bi'
    for path in sorted(index_root.glob('*/index.cypher')):
        number = int(path.parent.name[2:])
        entry = dict(file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        script = path.read_text()
        if number in PARAMETERIZED:
            query_path = W.DOCS / f'Neo4j/queries/baseline/ldbc-bi/bi-{number}.cypher'
            params = W.parameters('ldbc-bi', query_path, query_path.read_text())
            script = (path.parent / 'parameter.cypher').read_text()
            entry['parameter_file'] = str(path.parent / 'parameter.cypher')
        else:
            params = None
        with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
            try:
                registrations = mw.register_neo4j_index(script, parameters=params)
                entry['status'] = 'registered'
                entry['indexes'] = [r.index_id for r in registrations]
            except Exception as error:
                entry.update(status='unsupported', error=str(error))
        out.append(entry)
    return out


def run(args):
    from neo4j import GraphDatabase
    report = dict(docs_commit=subprocess.check_output(['git', '-C', str(W.DOCS), 'rev-parse', 'HEAD'], text=True).strip(),
                  workload_queries_executed=False, verification_enabled=True,
                  parameter_indexes='metadata only; no physical parameter index rebuild',
                  samples_per_query=args.samples, uri=args.uri, queries=[], index_inventory=[])
    report['index_inventory'] = inventory()
    paths = W.query_files('ldbc-bi', 'neo4j')
    if args.index_queries:
        numbers = {int(Path(row['file']).parent.name[2:]) for row in report['index_inventory']}
        paths = [p for p in paths if int(W.query_name(p).split('-')[1]) in numbers]
    report['scope'] = 'documented_neo4j_indexes' if args.index_queries else 'all_bi_queries'
    paths.sort(key=lambda p: int(W.query_name(p).split('-')[1]))
    with GraphDatabase.driver(args.uri, auth=None) as driver, driver.session() as session:
        driver.verify_connectivity()
        for path in paths:
            name = W.query_name(path)
            number = int(name.split('-')[1])
            source = Path(path).read_text()
            query = dict(query=name, source=str(path), sha256=hashlib.sha256(source.encode()).hexdigest(), samples=[])
            for sample in range(min(args.samples, W.samples('ldbc-bi', path))):
                params = W.parameters('ldbc-bi', path, source, sample)
                row = dict(sample=sample, parameters=params)
                with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
                    for n in STATIC:
                        mw.register_neo4j_index((W.DOCS / f'Neo4j/queries/index/ldbc-bi/bi{n}/index.cypher').read_text())
                    if number in PARAMETERIZED:
                        script = (W.DOCS / f'Neo4j/queries/index/ldbc-bi/bi{number}/parameter.cypher').read_text()
                        try:
                            registrations = mw.register_neo4j_index(script, parameters=params)
                            row['parameter_indexes'] = [r.index_id for r in registrations]
                        except Exception as error:
                            row['registration_error'] = str(error)
                    result = mw.execute(source, parameters=params)
                    row.update(rewritten=result.rewritten, reason=result.reason, detail=result.detail,
                               indexes=result.indexes, specialized=result.specialized, route=result.route,
                               proofs=result.proofs)
                    if result.rewritten:
                        row['cypher'] = result.cypher
                        used = {k: v.replace(tzinfo=datetime.timezone.utc)
                                if isinstance(v, datetime.datetime) and v.tzinfo is None else v
                                for k, v in params.items() if '$' + k in result.cypher}
                        try:
                            summary = session.run('EXPLAIN ' + result.cypher, used).consume()
                            row['explain'] = 'ok'
                            row['query_type'] = summary.query_type
                        except Exception as error:
                            row['explain'] = 'error'
                            row['error'] = str(error)
                    else:
                        row['original_preserved'] = result.cypher == source
                query['samples'].append(row)
            query['rewritten_samples'] = sum(row['rewritten'] for row in query['samples'])
            query['all_rewritten'] = all(row['rewritten'] for row in query['samples'])
            report['queries'].append(query)
            print(name, f'{query["rewritten_samples"]}/{len(query["samples"])}',
                  dict(collections.Counter(row['reason'] for row in query['samples'])), flush=True)
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    rows = [row for q in report['queries'] for row in q['samples']]
    report.update(total_queries=len(report['queries']), total_samples=len(rows),
                  all_samples_rewritten=sum(q['all_rewritten'] for q in report['queries']),
                  rewritten_samples=sum(row['rewritten'] for row in rows),
                  explain_ok=sum(row.get('explain') == 'ok' for row in rows),
                  invalid_rewrites=sum(row.get('explain') == 'error' or
                                       (not row['rewritten'] and not row['original_preserved']) for row in rows))
    report['all_rewritten'] = report['all_samples_rewritten'] == report['total_queries'] == len(paths)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    return 2 if report['invalid_rewrites'] else 0 if report['all_rewritten'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uri', default='bolt://127.0.0.1:7693')
    parser.add_argument('--samples', type=int, default=10)
    parser.add_argument('--index-queries', action='store_true',
                        help='Only the 15 queries with Neo4j indexes in the experiment docs')
    parser.add_argument('--output', required=True)
    sys.exit(run(parser.parse_args()))
