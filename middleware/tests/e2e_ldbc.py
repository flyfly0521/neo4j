"""Execute LDBC BI rewrites on a real NeuG database and compare results.

1. Copy a NeuG LDBC database, run the index script (DDL + FILL + manual
   setup statements) inside NeuG.
2. Start the middleware on the resulting schema and register the same script.
3. For each query: run the original with NeuG rewriting disabled (ground
   truth) and the middleware output with rewriting disabled, compare rows.

  python3 tests/e2e_ldbc.py --db ../ldbc_sf0.1 --index-script .../index.cypher \
      --queries '/tmp/cico_exp6/q/*.cypher' [--profile kuzu --params manifest.csv]
"""
import argparse
import glob
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bi_coverage import fill_placeholders, lookup, manifest_types, query_name  # noqa: E402
from cico import IndexMiddleware  # noqa: E402
from cico.statements import split_script  # noqa: E402
from e2e_neug import Exec  # noqa: E402


def manifest_values(path):
    import csv
    values = {}
    with open(path) as handle:
        for row in csv.DictReader(handle, delimiter='|'):
            values.setdefault(row['query'], {k.split(':')[0]: v for k, v in json.loads(row['parameters']).items()})
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--workdir', default='/tmp/cico_ldbc')
    parser.add_argument('--index-script', required=True)
    parser.add_argument('--queries', nargs='+', required=True)
    parser.add_argument('--profile', default='neug')
    parser.add_argument('--params')
    parser.add_argument('--reuse', action='store_true', help='reuse an already prepared workdir database')
    args = parser.parse_args()
    work = Path(args.workdir)
    db = work / 'db'
    script = Path(args.index_script).read_text()
    if not args.reuse:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        shutil.copytree(args.db, db)
        ex = Exec(db)
        for statement in split_script(script):
            mode = 'schema' if re.match(r'\s*(FILL|CREATE|ALTER|DROP|CHECKPOINT)', statement, re.I) else 'update'
            try:
                ex.run(statement, mode)
            except RuntimeError as error:
                print('setup statement failed:', str(error)[:200])
        ex.close()
    types = manifest_types(args.params) if args.params else {}
    values = manifest_values(args.params) if args.params else {}
    files = sorted({f for p in args.queries for f in glob.glob(p)})
    rows = []
    with IndexMiddleware(db / 'graph.yaml', profile=args.profile, engine_log=work / 'engine.log') as mw:
        for r in mw.execute_script(script):
            if not r.ok:
                print('middleware setup:', r.reason, r.detail[:160])
        for path in files:
            name = query_name(path)
            text = Path(path).read_text()
            if '{}' in text:
                text = fill_placeholders(text)
            ptypes = {k: v for k, v in lookup(types, name).items() if '$' + k in text}
            params = {k: v for k, v in lookup(values, name).items() if '$' + k in text}
            result = mw.execute(text, parameter_types=ptypes)
            rows.append((name, result, params))
    ex = Exec(db)
    ok = 0
    for name, result, params in rows:
        if not result.rewritten:
            print(f'{name:8} [{result.reason}]')
            continue
        try:
            truth = sorted(ex.run(result.original, params=params))
            got = sorted(ex.run(result.cypher, params=params))
            status = 'OK' if got == truth else 'WRONG'
        except RuntimeError as error:
            status, truth, got = 'EXEC_ERROR', [], str(error)[:300]
        ok += status == 'OK'
        print(f'{name:8} [{status}] {result.indexes} rows={len(truth)}')
        if status != 'OK':
            print('   expected', truth[:3], '\n   got     ', got[:3] if isinstance(got, list) else got)
    ex.close()
    rewritten = sum(1 for _, r, _ in rows if r.rewritten)
    print(f'\n{ok}/{rewritten} rewritten queries return identical results ({len(rows)} queries total)')


if __name__ == '__main__':
    main()
