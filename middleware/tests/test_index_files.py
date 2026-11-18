"""Real compiler tests; recording executors simulate completed target builds."""
import json
import sys
import tempfile
import subprocess
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware, IndexFileCatalog
from cico.schema import Schema
from cico.neo4j import neo4j_profile
from test_middleware import schema_yaml


FIRST = '''ALTER TABLE Person ADD IF NOT EXISTS cached INT64 DEFAULT 0;
FILL Person(cached) FROM (
 MATCH (p:Person)-[:LIVES_IN]->(c:City) RETURN p.id, count(c));'''
SECOND = '''ALTER TABLE Person ADD IF NOT EXISTS doubled INT64 DEFAULT 0;
FILL Person(doubled) FROM (MATCH (p:Person) RETURN p.id, p.cached * 2);'''
QUERY = 'MATCH (p:Person)-[:LIVES_IN]->(c:City) RETURN p.id AS personId, count(c) AS cnt'


class IndexFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'indexes.cypher'
        self.file.write_text(FIRST)
        self.state = self.root / 'state.json'

    def catalog(self, target='test-db-generation-1', parameters=None):
        mw = IndexMiddleware(Schema.from_text(schema_yaml()), profile=neo4j_profile(
            Path(__file__).resolve().parents[1] / 'profiles/hetionet-neo4j.yaml'))
        self.addCleanup(mw.close)
        return mw, IndexFileCatalog(mw, [self.file], self.state, target, parameters)

    def test_plan_cannot_enable_an_unbuilt_index(self):
        mw, catalog = self.catalog()
        report = catalog.reload()
        self.assertEqual(report['pending'], ['PERSON(cached)'])
        self.assertEqual(report['active'], [])
        self.assertFalse(mw.execute(QUERY).rewritten)
        self.assertFalse(self.state.exists())
        self.assertTrue(report['plans'][0]['fill_statements'])
        self.assertTrue(any('__neug_fill_state_n_rows_' in s and 'count(*)' in s
                            for s in report['plans'][0]['fill_statements']))

    def test_restart_restores_only_successful_receipts_without_building(self):
        mw, catalog = self.catalog()
        statements = []
        first = catalog.reload(statements.append)
        self.assertEqual(first['built'], ['PERSON(cached)'])
        self.assertTrue(statements)
        self.assertTrue(mw.execute(QUERY).rewritten)
        mw2, catalog2 = self.catalog()
        statements.clear()
        second = catalog2.reload(statements.append)
        self.assertEqual(statements, [])
        self.assertEqual(second['unchanged'], ['PERSON(cached)'])
        self.assertTrue(mw2.execute(QUERY).rewritten)

    def test_comments_whitespace_keyword_case_and_duplicate_files_do_not_rebuild(self):
        mw, catalog = self.catalog()
        original = catalog.reload(lambda s: None)
        self.file.write_text('// changed comment\n' + FIRST.replace('MATCH ', 'match\n  ')
                             .replace('RETURN ', 'return\n ').replace('count(c)', 'COUNT ( c )'))
        catalog.files.append(self.file)
        statements = []
        report = catalog.reload(statements.append)
        self.assertEqual(report['fingerprints'], original['fingerprints'])
        self.assertEqual(report['pending'], [])
        self.assertEqual(statements, [])

    def test_added_index_does_not_reset_previous_carrier(self):
        _, catalog = self.catalog()
        catalog.reload(lambda s: None)
        self.file.write_text(FIRST + SECOND)
        statements = []
        report = catalog.reload(statements.append)
        self.assertEqual(report['built'], ['PERSON(doubled)'])
        self.assertEqual(report['unchanged'], ['PERSON(cached)'])
        self.assertEqual(report['dependencies']['PERSON(doubled)'], ['PERSON(cached)'])
        self.assertFalse(any('SET cico_n.cached' in s or 'SET p.cached' in s for s in statements))

    def test_source_change_invalidates_consumers_and_builds_in_dependency_order(self):
        _, catalog = self.catalog()
        self.file.write_text(FIRST + SECOND)
        catalog.reload(lambda s: None)
        self.file.write_text(FIRST.replace('RETURN p.id', 'WHERE c.id > 1 RETURN p.id') + SECOND)
        report = catalog.reload(lambda s: None)
        self.assertEqual(report['changed'], ['PERSON(cached)', 'PERSON(doubled)'])
        self.assertEqual(report['built'], ['PERSON(cached)', 'PERSON(doubled)'])

    def test_default_and_parameter_changes_invalidate_build_receipts(self):
        _, catalog = self.catalog(parameters={'cutoff': 1})
        self.file.write_text(FIRST.replace('RETURN p.id', 'WHERE c.id > $cutoff RETURN p.id'))
        catalog.reload(lambda s: None)
        catalog.parameters['cutoff'] = 2
        report = catalog.reload(lambda s: None)
        self.assertEqual(report['built'], ['PERSON(cached)'])
        self.file.write_text(self.file.read_text().replace('DEFAULT 0', 'DEFAULT 7'))
        self.assertEqual(catalog.reload()['pending'], ['PERSON(cached)'])

    def test_invalid_reload_preserves_live_catalog_and_receipt(self):
        mw, catalog = self.catalog()
        catalog.reload(lambda s: None)
        before = self.state.read_bytes()
        self.file.write_text(FIRST + '; MATCH (p:Person) SET p.cached=3')
        with self.assertRaisesRegex(ValueError, 'expected only NeuG'):
            catalog.reload(lambda s: self.fail('Must fail before writes'))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(mw.execute(QUERY).rewritten)

    def test_failed_materialization_is_disabled_and_retry_skips_completed_upstream(self):
        mw, catalog = self.catalog()
        self.file.write_text(FIRST + SECOND)
        def fail_second(statement):
            if 'doubled' in statement:
                raise RuntimeError('target write failed')
        with self.assertRaisesRegex(RuntimeError, 'target write failed'):
            catalog.reload(fail_second)
        state = json.loads(self.state.read_text())['indexes']
        self.assertEqual(state['PERSON(cached)']['status'], 'ready')
        self.assertEqual(state['PERSON(doubled)']['status'], 'building')
        self.assertEqual(list(mw.fills), ['PERSON(cached)'])
        report = catalog.reload(lambda s: None)
        self.assertEqual(report['built'], ['PERSON(doubled)'])

    def test_other_physical_database_cannot_reuse_a_receipt(self):
        _, catalog = self.catalog()
        catalog.reload(lambda s: None)
        _, other = self.catalog(target='different-db-generation')
        with self.assertRaisesRegex(ValueError, 'physical target mismatch'):
            other.reload()

    def test_cycles_and_overlapping_writes_fail_before_any_build(self):
        _, catalog = self.catalog()
        self.file.write_text(FIRST + SECOND.replace('p.cached * 2', 'p.doubled * 2'))
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            catalog.reload(lambda s: self.fail('Must reject cycle before writes'))
        self.file.write_text(FIRST + '''
ALTER TABLE Person ADD IF NOT EXISTS extra INT64;
FILL Person(cached,extra) FROM (MATCH (p:Person) RETURN p.id,1,2);''')
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            catalog.reload()

    def test_removed_definition_stops_rewriting_without_deleting_physical_data(self):
        mw, catalog = self.catalog()
        catalog.reload(lambda s: None)
        self.file.write_text('// no indexes')
        statements = []
        report = catalog.reload(statements.append)
        self.assertEqual(report['removed'], ['PERSON(cached)'])
        self.assertEqual(statements, [])
        self.assertFalse(mw.execute(QUERY).rewritten)

    def test_string_literal_case_is_not_normalized_away(self):
        _, catalog = self.catalog()
        self.file.write_text('ALTER TABLE City ADD IF NOT EXISTS name STRING;\n' +
                             FIRST.replace('RETURN p.id', "WHERE c.name = 'Paris' RETURN p.id"))
        catalog.reload(lambda s: None)
        self.file.write_text(self.file.read_text().replace("'Paris'", "'PARIS'"))
        self.assertEqual(catalog.reload()['pending'], ['PERSON(cached)'])

    def test_nullable_count_is_not_enabled_without_group_presence_proof(self):
        _, catalog = self.catalog()
        self.file.write_text('ALTER TABLE City ADD IF NOT EXISTS score INT64;\n' +
                             FIRST.replace('count(c)', 'count(c.score)'))
        with self.assertRaisesRegex(ValueError, 'group-presence proof'):
            catalog.reload(lambda s: self.fail('Unsafe definition must fail before writes'))

    def test_managed_cli_plans_native_input_without_enabling_it(self):
        schema = self.root / 'schema.yaml'
        schema.write_text(schema_yaml())
        result = subprocess.run([sys.executable, '-m', 'cico', 'build', '--schema', str(schema),
                                 '--profile', 'neo4j', '--fill-file', str(self.file),
                                 '--index-state', str(self.state), '--index-target', 'offline-test', '--json'],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)[0]['ok'])
        self.assertEqual(json.loads(result.stderr)['active'], [])
        self.assertFalse(self.state.exists())

    def test_http_reload_and_unmanaged_mutation_guard(self):
        from cico.server import make_handler
        mw, catalog = self.catalog()
        catalog.reload(lambda s: None)
        writes = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(mw, catalog, writes.append))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.file.write_text(FIRST + SECOND)
            url = f'http://127.0.0.1:{server.server_port}'
            request = urllib.request.Request(url + '/index-files/reload', data=b'{}',
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
            self.assertEqual(result['built'], ['PERSON(doubled)'])
            with urllib.request.urlopen(url + '/index-files', timeout=10) as response:
                self.assertEqual(json.load(response)['pending'], [])
            request = urllib.request.Request(url + '/index', data=b'{"fills":[]}',
                                             headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(raised.exception.code, 400)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_target_validation_finishes_before_any_reset_or_catalog_change(self):
        mw, catalog = self.catalog()
        catalog.reload(lambda s: None)
        previous = self.state.read_bytes()
        self.file.write_text(FIRST + SECOND)
        test = self
        class InvalidTarget:
            def validate(self, statements):
                test.assertTrue(statements)
                raise ValueError('target syntax is unsupported')
            def __call__(self, statement):
                test.fail('Must EXPLAIN all statements before executing a reset')
        with self.assertRaisesRegex(ValueError, 'target syntax'):
            catalog.reload(InvalidTarget())
        self.assertEqual(self.state.read_bytes(), previous)
        self.assertEqual(list(mw.fills), ['PERSON(cached)'])


if __name__ == '__main__':
    unittest.main()
