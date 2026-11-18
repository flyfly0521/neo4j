"""End-to-end validation on the real target system (Kuzu or Neo4j).

For one workload:
  1. start the middleware on the target schema and register the NeuG FILLs;
  2. materialize every index on a copy of the target's baseline store using
     the middleware's own build statements (Materializer, target dialect);
  3. for every baseline query: rewrite it, run the original and the rewritten
     text on the target with the same parameters, compare the results.

  # Kuzu (run with a Python that has `kuzu`):
  python tests/target_validation.py --system kuzu --workload hetionet \
      --store /path/to/kuzu/hetionet-baseline --workdir /tmp/cico_tv
  # Neo4j (a running server on a copy of the store; Python with `neo4j`):
  python3 tests/target_validation.py --system neo4j --workload hetionet --uri bolt://127.0.0.1:7690
"""
import argparse
import datetime
import json
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware  # noqa: E402
import workloads as W  # noqa: E402
from workload_coverage import profile_for  # noqa: E402


# ---------------------------------------------------------------- targets
class KuzuTarget:
    def __init__(self, store, workdir, threads, reuse=False):
        import kuzu
        path = Path(workdir) / 'kuzu-db'
        if not (reuse and path.exists()):
            if path.exists():
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            shutil.copytree(store, path) if Path(store).is_dir() else shutil.copy(store, path)
        # bounded buffer pool: Kuzu defaults to most of the host's RAM (shared machine)
        self.db = kuzu.Database(str(path), buffer_pool_size=16 << 30)
        self.conn = kuzu.Connection(self.db, num_threads=threads)

    def run(self, cypher, params=None):
        result = self.conn.execute(cypher, parameters=params or {})
        rows = []
        while result.has_next():
            rows.append(result.get_next())
        return rows

    def close(self):
        self.conn.close()
        self.db.close()


class Neo4jTarget:
    def __init__(self, uri, user, password, database):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password) if password else None)
        self.database = database

    def run(self, cypher, params=None):
        with self.driver.session(database=self.database) as session:
            return [list(r.values()) for r in session.run(cypher, params or {})]

    def close(self):
        self.driver.close()


def neo4j_value(v):
    """Kuzu runner values -> Neo4j driver values (BI/IC stores keep zoned UTC datetimes)."""
    if isinstance(v, datetime.datetime) and v.tzinfo is None:
        return v.replace(tzinfo=datetime.timezone.utc)
    return v


# ------------------------------------------------------------- comparison
def norm(v):
    if isinstance(v, float):
        return ('f', 0.0 if v == 0 else float(f'{v:.9g}')) if not math.isnan(v) else ('nan',)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return ('t', v.isoformat()[:23])
    if isinstance(v, (list, tuple)):
        return ('l', tuple(norm(x) for x in v))
    if isinstance(v, dict):
        return ('d', tuple(sorted((k, norm(x)) for k, x in v.items())))
    if hasattr(v, 'iso_format'):  # neo4j temporal types
        return ('t', v.iso_format()[:23])
    if hasattr(v, 'element_id'):  # neo4j entities
        return ('e', v.element_id)
    return ('v', v)


def same(a, b):
    return sorted(map(lambda r: tuple(norm(x) for x in r), a)) == \
        sorted(map(lambda r: tuple(norm(x) for x in r), b))


# ------------------------------------------------------------------ main
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--system', required=True, choices=['kuzu', 'neo4j'])
    p.add_argument('--workload', required=True)
    p.add_argument('--store', help='Kuzu baseline store (copied, never modified)')
    p.add_argument('--workdir', default='/tmp/cico_tv')
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--uri', default='bolt://127.0.0.1:7690')
    p.add_argument('--user', default='neo4j')
    p.add_argument('--password', default='')
    p.add_argument('--database', default='neo4j')
    p.add_argument('--only', nargs='*')
    p.add_argument('--skip-build', action='store_true', help='indexes already built on the target')
    p.add_argument('--samples', type=int, default=1, help='parameter samples per query (manifest rows)')
    p.add_argument('--output')
    args = p.parse_args(argv)
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    report = dict(system=args.system, workload=args.workload, build=[], queries=[])
    target = (KuzuTarget(args.store, args.workdir, args.threads, reuse=args.skip_build) if args.system == 'kuzu'
              else Neo4jTarget(args.uri, args.user, args.password, args.database))
    convert = neo4j_value if args.system == 'neo4j' else (lambda v: v)
    with IndexMiddleware(W.schema_path(args.workload), profile=profile_for(args.system, args.workload)) as mw:
        for r in mw.execute_script(W.index_script(args.workload)):
            if not r.ok:
                print('index registration failed:', r.reason, r.detail[:200])
        # ---- build every index on the target with the middleware's statements
        built = mw.build_statements()
        if not args.skip_build:
            for phase in ('ddl', 'reset', 'fill_statements'):
                for entry in built:
                    for statement in entry[phase]:
                        start = time.time()
                        try:
                            target.run(statement)
                            status = 'ok'
                        except Exception as error:  # noqa: BLE001  report and continue
                            status = f'error: {str(error)[:300]}'
                            entry['ok'] = False
                        report['build'].append(dict(index_id=entry['index_id'], phase=phase, status=status,
                                                    seconds=round(time.time() - start, 2), statement=statement))
                        if status != 'ok':
                            print(f'[build] {entry["index_id"]} {phase}: {status}\n        {statement[:300]}')
        broken = {e['index_id'] for e in built if not e['ok']}
        print(f'built {len(built) - len(broken)}/{len(built)} FILLs on {args.system}'
              + (f'; not built: {sorted(broken)}' if broken else ''))
        # ---- rewrite and compare
        ok = total = 0
        for path in W.query_files(args.workload, args.system):
            name = W.query_name(path)
            if args.only and name not in args.only:
                continue
            text = Path(path).read_text()
            if W.is_write(text):
                continue
            for sample in range(min(args.samples, W.samples(args.workload, path))):
                params = W.parameters(args.workload, path, text, sample)
                r = mw.execute(text, parameters=params)
                entry = dict(query=name, sample=sample, reason=r.reason, indexes=r.indexes, specialized=r.specialized,
                             rewritten_cypher=r.cypher if r.rewritten else '')
                if r.rewritten:
                    total += 1
                    if set(r.indexes) & broken:
                        entry['status'] = 'SKIP_INDEX_NOT_BUILT'
                    else:
                        used = {k: convert(v) for k, v in params.items() if '$' + k in r.cypher}
                        try:
                            t0 = time.time()
                            truth = target.run(text, {k: convert(v) for k, v in params.items()})
                            t1 = time.time()
                            got = target.run(r.cypher, used)
                            t2 = time.time()
                            entry.update(status='OK' if same(truth, got) else 'WRONG', rows=len(truth),
                                         original_s=round(t1 - t0, 3), rewritten_s=round(t2 - t1, 3))
                            if entry['status'] == 'WRONG':
                                entry.update(expected=repr(truth[:3])[:400], got=repr(got[:3])[:400])
                        except Exception as error:  # noqa: BLE001
                            entry.update(status='EXEC_ERROR', error=str(error)[:400])
                    ok += entry.get('status') == 'OK'
                    print(f'{name + "#" + str(sample):28} {entry["status"]:20} rows={entry.get("rows", "-")} '
                          f'{entry.get("original_s", "")}s -> {entry.get("rewritten_s", "")}s {r.indexes}'
                          + (f' specialized={r.specialized}' if r.specialized else ''))
                    for key in ('expected', 'got', 'error'):
                        if key in entry:
                            print(f'    {key}: {entry[key]}')
                    if entry['status'] != 'OK':
                        print(f'    rewritten: {r.cypher[:600]}')
                report['queries'].append(entry)
        print(f'\n{args.system}/{args.workload}: {ok}/{total} rewritten queries return identical results')
    target.close()
    if args.output:
        json.dump(report, open(args.output, 'w'), indent=1, default=str, ensure_ascii=False)


if __name__ == '__main__':
    main()
