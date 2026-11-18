"""LDBC BI coverage: feed an index script (DDL + FILL) into the middleware at
runtime, then rewrite the BI queries of one system and report the outcome.

  python3 tests/bi_coverage.py --profile kuzu \
     --schema ../ldbc_sf0.1/graph.yaml --stats STATS.json \
     --index-script IncGQ-experiment-docs/NeuG/queries/index/ldbc-bi/index.cypher \
     --queries 'IncGQ-experiment-docs/Kuzu/queries/baseline/ldbc-bi/bi-*.cypher' \
     --params IncGQ-experiment-docs/parameters/bi-sf1/experiment-benchmark.csv \
     --output /tmp/cico_bi_kuzu.json
"""
import argparse
import collections
import csv
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware  # noqa: E402

TYPE = {'DATETIME': 'TIMESTAMP', 'DATE': 'DATE', 'STRING': 'STRING', 'ID': 'INT64', 'LONG': 'INT64',
        'INT': 'INT64', 'INT32': 'INT64', 'STRING[]': 'STRING[]', 'DOUBLE': 'DOUBLE'}


def query_name(path):
    p = Path(path)
    stem = p.parent.name if p.name in ('query.cypher', 'baseline.cypher') else p.stem
    m = re.search(r'bi-?(\d+)([a-z]?)', stem)
    return f'bi-{m[1]}{m[2]}' if m else stem


def manifest_types(path):
    """query -> {param: type} from the shared parameter manifest (first sample)."""
    types = {}
    if not path:
        return types
    with open(path) as handle:
        for row in csv.DictReader(handle, delimiter='|'):
            if row['query'] in types:
                continue
            params = json.loads(row['parameters'])
            types[row['query']] = {k.split(':')[0]: TYPE.get(k.split(':')[1], 'ANY') if ':' in k else 'ANY'
                                   for k in params}
    return types


def lookup(table, name):
    """Manifest keys carry variant suffixes: bi-14 -> bi-14a."""
    for key in (name, name + 'a', name.rstrip('ab')):
        if key in table:
            return table[key]
    return {}


def fill_placeholders(text):
    """NeuG baseline templates use positional '{}' placeholders."""
    text = re.sub(r"TIMESTAMP\('\{\}( 00:00:00)?'\)", "TIMESTAMP('2012-06-01 00:00:00')", text)
    text = text.replace("INTERVAL('{} HOURS')", "INTERVAL('4 HOURS')")
    return text.replace('{}', 'India')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', default='neug')
    parser.add_argument('--schema', required=True)
    parser.add_argument('--stats')
    parser.add_argument('--index-script', required=True)
    parser.add_argument('--queries', nargs='+', required=True)
    parser.add_argument('--params')
    parser.add_argument('--only', nargs='*', help='query names to keep, e.g. bi-3 bi-14a')
    parser.add_argument('--output', required=True)
    parser.add_argument('--engine-log')
    args = parser.parse_args(argv)
    types = manifest_types(args.params)
    files = sorted({f for pattern in args.queries for f in glob.glob(pattern)})
    files = [f for f in files if not re.search(r'(setup|create-graph|drop-graph|without-precomputation)', f)]
    if args.only:
        files = [f for f in files if query_name(f) in args.only]
    report = dict(profile=args.profile, index_statements=[], queries=[])
    with IndexMiddleware(args.schema, args.stats, profile=args.profile, engine_log=args.engine_log) as mw:
        for result in mw.execute_script(Path(args.index_script).read_text()):
            report['index_statements'].append(dict(kind=result.kind, ok=result.ok, reason=result.reason,
                                                   index_id=result.index_id, detail=result.detail[:300],
                                                   inferred_ddl=result.inferred_ddl,
                                                   statement=result.original[:160]))
        reasons = collections.Counter()
        for path in files:
            text = Path(path).read_text()
            name = query_name(path)
            if '{}' in text:
                text = fill_placeholders(text)
            ptypes = {p: t for p, t in lookup(types, name).items() if '$' + p in text}
            result = mw.execute(text, parameter_types=ptypes)
            reasons[result.reason] += 1
            report['queries'].append(dict(query=name, file=path, **result.to_dict()))
            print(f'{name:8} {result.reason:28} {",".join(result.indexes):60} {result.detail[:160]}')
        report['summary'] = dict(queries=len(files), reasons=dict(reasons),
                                 registered=sum(1 for s in report['index_statements'] if s['kind'] == 'fill' and s['ok']),
                                 index_errors=[s for s in report['index_statements'] if not s['ok']])
    json.dump(report, open(args.output, 'w'), indent=2, ensure_ascii=False)
    print(json.dumps(report['summary'], indent=2, ensure_ascii=False)[:3000])


if __name__ == '__main__':
    main()
