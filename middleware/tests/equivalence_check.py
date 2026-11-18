"""Check an edited docs query against the original on the real target system.

For every parameter sample: run the original docs query, the modified text
and the middleware's rewrite of the modified text; all three must return the
same multiset of rows.  Indexes must already be built on the target store
(tests/target_validation.py builds them).

  python tests/equivalence_check.py --system kuzu --workload ldbc-bi --query bi-4 \
      --modified /tmp/bi-4.cypher --store /path/to/kuzu-db --samples 10
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware  # noqa: E402
import workloads as W  # noqa: E402
from workload_coverage import profile_for  # noqa: E402
from target_validation import Neo4jTarget, neo4j_value, same  # noqa: E402


class KuzuStore:
    def __init__(self, path, threads):
        import kuzu
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


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--system', required=True, choices=['kuzu', 'neo4j'])
    p.add_argument('--workload', required=True)
    p.add_argument('--query', required=True)
    p.add_argument('--original', help='original text (default: the docs file of --query)')
    p.add_argument('--modified', required=True)
    p.add_argument('--store', help='Kuzu store with the indexes built')
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--uri', default='bolt://127.0.0.1:7690')
    p.add_argument('--samples', type=int, default=10)
    args = p.parse_args(argv)
    path = next(f for f in W.query_files(args.workload, args.system) if W.query_name(f) == args.query)
    original = Path(args.original or W.docs_file(path)).read_text()
    modified = Path(args.modified).read_text()
    target = KuzuStore(args.store, args.threads) if args.system == 'kuzu' else \
        Neo4jTarget(args.uri, 'neo4j', '', 'neo4j')
    convert = neo4j_value if args.system == 'neo4j' else (lambda v: v)
    failures = 0
    with IndexMiddleware(W.schema_path(args.workload), profile=profile_for(args.system, args.workload)) as mw:
        list(mw.execute_script(W.index_script(args.workload)))
        for sample in range(min(args.samples, W.samples(args.workload, path))):
            params = W.parameters(args.workload, path, original, sample)
            r = mw.execute(modified, parameters=params)
            texts = [('original', original), ('modified', modified)] + \
                ([('rewritten', r.cypher)] if r.rewritten else [])
            rows, times = {}, {}
            for name, text in texts:
                used = {k: convert(v) for k, v in params.items() if '$' + k in text}
                t0 = time.time()
                rows[name] = target.run(text, used)
                times[name] = round(time.time() - t0, 3)
            ok = all(same(rows['original'], rows[n]) for n, _ in texts[1:])
            failures += not ok
            print(f"{args.query}#{sample} {'OK' if ok else 'DIFFERENT'} rows={len(rows['original'])} "
                  + ' '.join(f'{n}={t}s' for n, t in times.items())
                  + ('' if r.rewritten else f' (modified not rewritten: {r.reason})'))
            if not ok:
                for n, _ in texts:
                    print(f'    {n}: {repr(rows[n][:3])[:300]}')
    target.close()
    print('equivalent on all samples' if not failures else f'{failures} samples differ')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
