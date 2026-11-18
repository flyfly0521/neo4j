"""CLI.

  python3 -m cico rewrite --schema S.yaml [--profile neug|kuzu|neo4j] [--index-script I.cypher] \
        --query "MATCH ..." [--param-types '{"tag":"STRING"}'] [--json]
  python3 -m cico script  --schema S.yaml [--profile ...] --file statements.cypher   # DDL/FILL/queries
  python3 -m cico serve   --schema S.yaml [--profile ...] [--index-script I.cypher] [--port 8765]
  python3 -m cico build   --schema S.yaml --profile kuzu|neo4j --index-script I.cypher [--json]
        # target-dialect statements that materialize the indexes (DDL, reset, fill)
  python3 -m cico schema  --kuzu DB [--reference graph.yaml] [--output S.yaml]   # schema from a Kuzu store
"""
import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from .middleware import IndexMiddleware


def _common(parser):
    parser.add_argument('--schema', required=True, help='NeuG schema YAML of the target graph')
    parser.add_argument('--stats', help='NeuG statistics JSON (cost-based index choice)')
    parser.add_argument('--profile', default='neug', help='neug | kuzu | neo4j')
    parser.add_argument('--index-script', help='DDL/FILL script registered at startup')
    parser.add_argument('--index-script-dialect', choices=('profile', 'neug'), default='profile',
                        help='Explicit input dialect for --index-script; output still uses --profile')
    parser.add_argument('--fill-file', action='append', default=[],
                        help='Managed NeuG DDL/FILL file; repeat in declaration order')
    parser.add_argument('--index-state', help='Durable successful-build receipts for --fill-file')
    parser.add_argument('--index-target', help='Physical database generation identity (offline planning only)')
    parser.add_argument('--fill-params', default='{}', help='JSON scalar parameters to inline into managed FILLs')
    parser.add_argument('--neo4j-uri', help='Neo4j target; identity is checked using db.info()')
    parser.add_argument('--neo4j-database', default='neo4j')
    parser.add_argument('--neo4j-user', help='Password read from CICO_NEO4J_PASSWORD; omit for auth-disabled DB')
    parser.add_argument('--materialize-new', action='store_true',
                        help='Build only pending managed indexes on --neo4j-uri before enabling them')
    parser.add_argument('--neo4j-index-script', action='append', default=[],
                        help='Neo4j materialization script (MATCH/SET or derived MERGE); repeatable')
    parser.add_argument('--no-verify', action='store_true')
    parser.add_argument('--engine')
    parser.add_argument('--engine-log')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'schema':
        from .introspect import main as schema_main
        return schema_main(argv[1:])
    parser = argparse.ArgumentParser(prog='cico')
    commands = parser.add_subparsers(dest='command', required=True)
    rewrite = commands.add_parser('rewrite')
    _common(rewrite)
    rewrite.add_argument('--query', required=True)
    rewrite.add_argument('--param-types', default='{}')
    rewrite.add_argument('--json', action='store_true')
    script = commands.add_parser('script')
    _common(script)
    script.add_argument('--file', required=True)
    build = commands.add_parser('build')
    _common(build)
    build.add_argument('--json', action='store_true')
    server = commands.add_parser('serve')
    _common(server)
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)

    if args.fill_file and (not args.index_state or not (args.index_target or args.neo4j_uri)):
        parser.error('--fill-file requires --index-state and --neo4j-uri (or --index-target for offline plans)')
    if args.fill_file and (args.index_script or args.neo4j_index_script):
        parser.error('Use managed --fill-file separately from unmanaged index scripts')
    if args.materialize_new and (not args.fill_file or not args.neo4j_uri or args.profile != 'neo4j'):
        parser.error('--materialize-new requires --fill-file, --neo4j-uri and --profile neo4j')
    if args.fill_file and args.command == 'script':
        parser.error('Managed FILL catalogs do not accept mixed runtime scripts')

    with ExitStack() as stack:
        mw = stack.enter_context(IndexMiddleware(args.schema, args.stats, profile=args.profile, verify=not args.no_verify,
                                                engine_binary=args.engine, engine_log=args.engine_log))
        catalog, executor = None, None
        if args.fill_file:
            from .index_files import IndexFileCatalog, Neo4jIndexExecutor
            target = args.index_target
            if args.neo4j_uri:
                auth = (args.neo4j_user, os.environ['CICO_NEO4J_PASSWORD']) if args.neo4j_user else None
                runner = Neo4jIndexExecutor(args.neo4j_uri, args.neo4j_database, auth)
                stack.callback(runner.close)
                target = runner.target
                if args.materialize_new:
                    executor = runner
            catalog = IndexFileCatalog(mw, args.fill_file, args.index_state, target,
                                       parameters=json.loads(args.fill_params))
            report = catalog.reload(executor)
            print(json.dumps({k: v for k, v in report.items() if k != 'plans'}, ensure_ascii=False), file=sys.stderr)
        for path in args.neo4j_index_script:
            mw.register_neo4j_index(Path(path).read_text())
        if args.index_script:
            text = Path(args.index_script).read_text()
            if args.index_script_dialect == 'neug':
                from .statements import classify, split_script
                for statement in split_script(text):
                    kind = classify(statement)
                    if kind == 'ddl':
                        mw.apply_ddl(statement, input_dialect='neug')
                    elif kind == 'fill':
                        mw.register_index([statement], input_dialect='neug')
                    elif kind != 'noop':
                        raise ValueError('--index-script-dialect neug accepts only DDL/FILL')
            else:
                for result in mw.execute_script(text):
                    if not result.ok:
                        print(f'warning: {result.reason}: {result.detail[:200]}', file=sys.stderr)
        if args.command == 'rewrite':
            result = mw.execute(args.query, parameter_types=json.loads(args.param_types))
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(result.cypher)
                print(f'-- {result.reason} {result.indexes} {result.detail[:300]}'.rstrip(), file=sys.stderr)
        elif args.command == 'build':
            built = (catalog.last_report['plans'] if catalog else
                     mw.build_statements(include_internal_state=args.index_script_dialect == 'neug'))
            if args.json:
                print(json.dumps(built, indent=2, ensure_ascii=False))
            else:
                for phase in ('ddl', 'reset', 'fill_statements'):
                    print(f'// ---- {phase}')
                    for entry in built:
                        if not entry['ok'] and phase == 'ddl':
                            print(f"// {entry['index_id']}: not materializable: {entry['error']}")
                        for statement in entry[phase]:
                            print(statement + ';')
        elif args.command == 'script':
            for result in mw.execute_script(Path(args.file).read_text()):
                print(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            from .server import serve
            print(f'cico middleware ({args.profile}) on http://{args.host}:{args.port}', file=sys.stderr)
            serve(mw, args.host, args.port, catalog=catalog, index_executor=executor)
    return 0


if __name__ == '__main__':
    sys.exit(main())
