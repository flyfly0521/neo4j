"""Corpus regression: run probe catalogs (FILL candidates + original queries)
through the middleware and report how many queries are rewritten, verified,
or fall back, with the reasons.

  python3 tests/corpus_check.py --catalogs DIR [DIR ...] --schema S.yaml [--stats st.json] \
      --output /tmp/cico_corpus.json [--limit N]

Every DIR must contain an input.json in check_index_rewrites format.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalogs', nargs='+', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--stats')
    parser.add_argument('--output', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--use-input-stats', action='store_true',
                        help='use stats_path recorded in each input.json')
    args = parser.parse_args(argv)
    folders = [Path(p) for p in args.catalogs if (Path(p) / 'input.json').is_file()]
    if args.limit:
        folders = folders[:args.limit]
    reasons, details, rows = collections.Counter(), collections.defaultdict(list), []
    for number, folder in enumerate(folders):
        request = json.load(open(folder / 'input.json'))
        stats = request.get('stats_path') if args.use_input_stats else args.stats
        with IndexMiddleware(args.schema, stats) as mw:
            for candidate in request['candidates']:
                try:
                    mw.register_index(candidate['fills'], candidate['id'])
                except Exception as error:  # report, continue with other candidates
                    reasons['index_registration_failed'] += 1
                    details['index_registration_failed'].append(str(error)[:200])
            for query in request['queries']:
                result = mw.execute(query['cypher'], parameter_types=query.get('parameter_types') or {})
                reasons[result.reason] += 1
                if result.detail:
                    details[result.reason].append(result.detail[:600])
                rows.append(dict(catalog=folder.name, id=query['id'], **result.to_dict()))
        print(f'[{number + 1}/{len(folders)}] {folder.name}: {dict(reasons)}', file=sys.stderr)
    summary = dict(catalogs=len(folders), queries=len(rows), reasons=dict(reasons),
                   detail_samples={k: collections.Counter(v).most_common(8) for k, v in details.items()})
    json.dump(dict(summary=summary, results=rows), open(args.output, 'w'), indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
