"""Compile experiment-docs NeuG index files for Neo4j, without building them."""
import argparse
import collections
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware, IndexFileCatalog
from cico.index_files import Neo4jIndexExecutor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docs', default='/mnt/sdb1/feiyang/IncGQ-experiment-docs')
    parser.add_argument('--output', required=True)
    parser.add_argument('--uri', help='EXPLAIN generated Neo4j statements, without execution')
    args = parser.parse_args()
    docs = Path(args.docs)
    root = Path(__file__).resolve().parents[1]
    report = {'docs_commit': subprocess.check_output(['git', '-C', str(docs), 'rev-parse', 'HEAD'], text=True).strip(),
              'registration_input': 'neug', 'target_profile': 'neo4j', 'physical_builds_executed': False,
              'workload_queries_executed': False, 'scope': 'NeuG/queries/index/ldbc-bi/*/index.cypher',
              'parameter_files_tested': False, 'files': []}
    runner = Neo4jIndexExecutor(args.uri) if args.uri else None
    try:
        with tempfile.TemporaryDirectory(prefix='cico-native-audit-') as tmp:
            for path in sorted((docs / 'NeuG/queries/index/ldbc-bi').glob('*/index.cypher')):
                row = {'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                try:
                    with IndexMiddleware(root / 'schemas/ldbc-bi.yaml', profile='neo4j') as mw:
                        catalog = IndexFileCatalog(mw, [path], Path(tmp) / (path.parent.name + '.json'), 'offline-audit')
                        result = catalog.reload()
                        row.update(status='planned' if result['pending'] else 'schema_only',
                                   indexes=result['pending'], plans=result['plans'], dependencies=result['dependencies'])
                        if runner is not None and result['pending']:
                            statements = [s for item in result['plans'] for phase in ('ddl', 'reset', 'fill_statements')
                                          for s in item[phase]]
                            runner.validate(statements)
                            row.update(neo4j_explain='ok', explain_statements=len(statements))
                except Exception as error:
                    row.update(status='unsupported', error=f'{type(error).__name__}: {error}')
                report['files'].append(row)
                print(path.parent.name, row['status'], row.get('indexes', []), row.get('error', '')[:180], flush=True)
    finally:
        if runner is not None:
            runner.close()
    report['counts'] = dict(collections.Counter(row['status'] for row in report['files']))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
