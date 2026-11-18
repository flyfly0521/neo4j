"""Status matrix: NeuG FILL files (experiment docs) -> original Neo4j BI queries.

For every NeuG/queries/index/ldbc-bi/<case>/ directory, register its DDL and
FILL statements (parameter files rendered with SF1 sample 0, as the
diagnosis does) in a fresh middleware, then rewrite the original
Neo4j/queries/baseline/ldbc-bi/bi-N.cypher once.  Compile only: no index is
built and no BI query is executed.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import workloads as W
from cico import IndexMiddleware
from cico.middleware import inline_parameters
from cico.neo4j import neo4j_profile

ROOT = Path(__file__).resolve().parents[1]


def statements(text):
    text = '\n'.join(line for line in text.splitlines() if not line.strip().startswith('//'))
    return [s.strip() for s in text.split(';') if s.strip()]


def neug_parameters(number, params):
    out = dict(params)
    if number == 2:
        out.update(windowStart=params['date'], windowMid=params['date'] + datetime.timedelta(days=100),
                   windowEnd=params['date'] + datetime.timedelta(days=200))
    if number == 16:
        for suffix in ('A', 'B'):
            start = params[f'date{suffix}'].replace(hour=0, minute=0, second=0, microsecond=0)
            out.update({f'date{suffix}Start': start, f'date{suffix}End': start + datetime.timedelta(days=1)})
    return out


def bi15w(text, params):
    """Fill the positional placeholders documented in bi15w/index.cypher."""
    day = lambda d: d.strftime('%Y-%m-%d %H:%M:%S')
    start, end = day(params['startDate']), day(params['endDate'])
    values = ['bi15w_reply_fwd', 'bi15w_post_fwd', start, end, 'bi15w_reply_rev', 'bi15w_post_rev', start, end,
              'bi15w_weight', 'bi15w_reply_fwd', 'bi15w_reply_rev', 'bi15w_post_fwd', 'bi15w_post_rev']
    ddl = ''.join(f'ALTER TABLE KNOWS ADD IF NOT EXISTS {c} INT64 DEFAULT 0;\n'
                  for c in values[:2] + values[4:6]) + 'ALTER TABLE KNOWS ADD IF NOT EXISTS bi15w_weight DOUBLE DEFAULT 1.0;\n'
    return ddl + text.replace('{}', '\x00').replace('\x00', '{}').format(*values)


def run(output):
    docs = W.DOCS
    report = dict(docs_commit=subprocess.check_output(['git', '-C', str(docs), 'rev-parse', 'HEAD'], text=True).strip(),
                  middleware_commit=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                                            text=True).strip(),
                  sample=0, indexes_built=False, workload_queries_executed=False, cases=[])
    for directory in sorted((docs / 'NeuG/queries/index/ldbc-bi').iterdir(), key=lambda p: p.name):
        if not directory.is_dir():
            continue
        number = int(re.match(r'bi(\d+)', directory.name).group(1))
        query_path = docs / f'Neo4j/queries/baseline/ldbc-bi/bi-{number}.cypher'
        query = query_path.read_text()
        params = W.parameters('ldbc-bi', query_path, query)
        row = dict(case=directory.name, query=query_path.name, parameters={k: str(v) for k, v in params.items()},
                   registration=[])
        started = time.time()
        with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
            for path in sorted(directory.glob('*.cypher'), key=lambda p: p.name != 'index.cypher'):
                text = path.read_text()
                if directory.name == 'bi15w':
                    text = bi15w(text, params)
                elif path.name == 'parameter.cypher':
                    if number == 13:   # positional, as NeuG/scripts/benchmark/run_workload.py renders it
                        text = text.replace('{}', params['endDate'].strftime('%Y-%m-%d'))
                    text = inline_parameters(text, neug_parameters(number, params))
                for s in statements(text):
                    if re.match(r'\s*MATCH\b', s, re.I):
                        continue    # reset statements (MATCH ... SET x = 0) are build steps, not definitions
                    try:
                        if s.upper().startswith('FILL'):
                            r = mw.register_index([s], input_dialect='neug')
                        else:
                            r = mw.apply_ddl(s, input_dialect='neug')
                        row['registration'].append(dict(file=path.name, ok=r.ok, reason=r.reason,
                                                        detail=(r.detail or '')[:200]))
                    except Exception as error:  # noqa: BLE001 - record and continue
                        row['registration'].append(dict(file=path.name, ok=False, reason=type(error).__name__,
                                                        detail=str(error)[:200]))
            r = mw.execute(query, parameters=params)
            row.update(rewritten=r.rewritten, reason=r.reason, indexes=r.indexes, route=r.route,
                       specialized=r.specialized, proofs=r.proofs, premises=r.premises,
                       detail=(r.detail or '')[:1500], output=r.cypher if r.rewritten else '',
                       engine_restarts=mw.engine_restarts, seconds=round(time.time() - started, 1),
                       indexes_registered=sorted(mw.fills), fills={k: list(v) for k, v in mw.fills.items()},
                       query_text=query)
        report['cases'].append(row)
        print(f"{directory.name:7} {query_path.name:14} {str(r.rewritten):5} {r.reason:24} "
              f"{','.join(r.indexes)[:60]:60} {row['seconds']}s", flush=True)
    Path(output).write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    run(p.parse_args().output)
