"""Rewrite coverage of the IncGQ-experiment-docs baseline queries.

For each workload: start the middleware on the NeuG schema, register the
workload's NeuG FILL scripts at runtime, then rewrite every Kuzu / Neo4j
baseline query and record the outcome.

  python3 tests/workload_coverage.py [--workloads hetionet lsqb ...] \
      [--systems kuzu neo4j] [--stats] --output /tmp/cico_cov.json
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware  # noqa: E402
import workloads as W  # noqa: E402


def profile_for(system, workload):
    if system == 'neo4j':
        from cico.neo4j import neo4j_profile
        return neo4j_profile(W.mapping_path(workload))
    return system


def run(workload, system, stats=None, engine_log=None, only=None, variant='baseline', scope='part'):
    out = dict(workload=workload, system=system, index=[], queries=[])
    with IndexMiddleware(W.schema_path(workload), stats, profile=profile_for(system, workload),
                         engine_log=engine_log, scope=scope) as mw:
        for r in mw.execute_script(W.index_script(workload)):
            if r.kind in ('fill', 'ddl') and not r.ok:
                out['index'].append(dict(reason=r.reason, detail=r.detail[:300], statement=r.original[:200]))
        for path in W.query_files(workload, system, variant):
            name = W.query_name(path)
            if only and name not in only:
                continue
            text = Path(path).read_text()
            if W.is_write(text):
                out['queries'].append(dict(query=name, file=path, reason='write_query', rewritten=False))
                continue
            params = W.parameters(workload, path, text)
            r = mw.execute(text, parameters=params)
            out['queries'].append(dict(query=name, file=path, **r.to_dict()))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--workloads', nargs='*', default=list(W.WORKLOADS))
    parser.add_argument('--systems', nargs='*', default=['kuzu', 'neo4j'])
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--variant', default='baseline', help='baseline | optimized (query directory)')
    parser.add_argument('--stats', help='NeuG statistics JSON (cost-based choice)')
    parser.add_argument('--output', required=True)
    parser.add_argument('--engine-log')
    parser.add_argument('--scope', default='part', choices=['part', 'query'])
    args = parser.parse_args(argv)
    report = []
    for workload in args.workloads:
        for system in args.systems:
            result = run(workload, system, args.stats, args.engine_log, args.only, args.variant, args.scope)
            report.append(result)
            reasons = collections.Counter(q['reason'].split(':')[0] for q in result['queries'])
            print(f'== {workload} / {system}: {dict(reasons)}  index errors: {len(result["index"])}')
            for e in result['index']:
                print(f'   [index] {e["reason"]}: {e["detail"][:150]} :: {e["statement"][:80]}')
            for q in result['queries']:
                if q['reason'] in ('write_query',):
                    continue
                detail = (q.get('detail') or '').replace('\n', ' ')[:150]
                parts = ('p' + ','.join(map(str, q['parts']))) if q.get('parts') else ''
                print(f'   {q["query"]:28} {q["reason"]:26} {parts:6} {",".join(q.get("indexes") or [])[:50]:50} {detail}')
    json.dump(report, open(args.output, 'w'), indent=1, ensure_ascii=False, default=str)


if __name__ == '__main__':
    main()
