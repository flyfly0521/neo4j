"""Declarative NeuG FILL files, compiled for a target profile.

Registration is metadata. Only a durable successful materialization receipt
allows a definition to enter the managed rewrite catalog. Reload compiles a
fresh catalog before touching either the live catalog or the database.
"""
import copy
import datetime
import fcntl
import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path

from .engine import Engine
from .middleware import IndexMiddleware, inline_parameters
from .neo4j import tokenize
from .statements import classify, parse_fill, split_script, strip_comments


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _tokens(text):
    # Plans are printed by the compiler, which already normalizes keyword
    # case. Do NOT lowercase identifiers or string literals here.
    return [(t.kind, t.text) for t in tokenize(strip_comments(text))]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class IndexFileCatalog:
    """Own a middleware catalog loaded from ordered NeuG DDL/FILL files.

    ``target`` must identify a physical database generation, not just a host.
    ``reload(executor=None)`` prepares a plan and restores previously built
    definitions. Supply an executor(statement) to build only pending indexes.
    It must block until each auto-commit statement finishes successfully.
    """

    VERSION = 1

    def __init__(self, middleware, files, state_file, target, parameters=None):
        if middleware.fills:
            raise ValueError('Create the file catalog before registering other indexes')
        if not target:
            raise ValueError('A physical target database identity is required')
        self.mw = middleware
        self.files = [Path(p).expanduser().resolve() for p in files]
        if not self.files:
            raise ValueError('At least one FILL file is required')
        self.state_file = Path(state_file).expanduser().resolve()
        self.target = target
        self.parameters = dict(parameters or {})
        self.base_schema = middleware.schema.copy()
        self.last_report = None

    def _read_state(self):
        if not self.state_file.exists():
            return {'version': self.VERSION, 'target': self.target, 'indexes': {}}
        state = json.loads(self.state_file.read_text())
        if state.get('version') != self.VERSION or state.get('target') != self.target:
            raise ValueError('Index state version/physical target mismatch; use a separate state file')
        if not isinstance(state.get('indexes'), dict):
            raise ValueError('Invalid index state: indexes must be an object')
        return state

    def _save(self, state):
        # Same-directory atomic replace; a crash never turns a partial JSON
        # file into a successful build receipt.
        fd, name = tempfile.mkstemp(prefix=self.state_file.name + '.', dir=self.state_file.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.state_file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _stage(self):
        mw = self.mw
        stage = IndexMiddleware(self.base_schema.copy(), profile=copy.deepcopy(mw.profile),
                                verify=mw.verify, engine_binary=mw._engine_args[0],
                                engine_log=mw._engine_args[1], specialize=mw.specialize, scope=mw.scope)
        try:
            groups = OrderedDict()
            # Replay declarations only in a private schema. DROP/ALTER never
            # reach the target. Files may contain no ordinary writes/queries.
            for path in self.files:
                for statement in split_script(path.read_text()):
                    kind = classify(statement)
                    if kind == 'ddl':
                        stage.apply_ddl(statement, input_dialect='neug')
                    elif kind == 'fill':
                        text = inline_parameters(statement, self.parameters)
                        if any(t.kind == 'param' for t in tokenize(text)):
                            raise ValueError(f'{path}: unbound FILL parameters')
                        fill = parse_fill(text)
                        if fill is None:
                            raise ValueError(f'{path}: invalid FILL statement')
                        key = (fill.table.upper(), tuple(fill.columns))
                        groups.setdefault(key, []).append(text)
                    elif kind != 'noop':
                        raise ValueError(f'{path}: expected only NeuG DDL/FILL, got {statement[:100]}')
            # Group same-carrier branches as one index (and deduplicate shared
            # definitions). Identifiers/literals remain case sensitive.
            owners = {}
            for (table, columns), fills in groups.items():
                _, carrier = stage.schema.table(table)
                outputs = columns or tuple(p['property_name'] for p in (carrier or {}).get('properties') or [])
                for column in outputs:
                    key = (table, column)
                    if key in owners:
                        raise ValueError(f'Overlapping FILL carriers: {owners[key]} and {table}{columns}')
                    owners[key] = (table, columns)
                unique = OrderedDict()
                for text in fills:
                    fill = parse_fill(text)
                    bound = stage.engine.bind(fill.source)
                    self._check_count_presence(stage, bound['ir'])
                    from .printer import Printer
                    from .dialects import get_dialect
                    dialect = get_dialect('neug')
                    dialect.set_schema(stage.schema)
                    ir = copy.deepcopy(bound['ir'])
                    projection = ir['single_queries'][0]['parts'][-1]['projection']
                    printer = Printer(dialect)
                    entity_names = printer._assign_names(ir)
                    occupied = {t.text.strip('`') for t in tokenize(fill.source) if t.kind in ('ident', 'quoted')}
                    names = []
                    for i, expression in enumerate(projection['expressions']):
                        name = f'cico_fill_{i}'
                        while name in occupied:
                            name += '_'
                        if 'entity' in expression:
                            name = entity_names[expression['entity']]
                        occupied.add(name)
                        names.append(name)
                    projection['names'] = names
                    printed = printer.print(ir).cypher
                    # Register this stable source too: NeuG derives hidden
                    # state column names from source text, not its bound IR.
                    canonical_fill = f'FILL {dialect.ident(table)}({",".join(dialect.ident(c) for c in columns)}) FROM ({printed})'
                    unique.setdefault(_digest(_tokens(printed)), canonical_fill)
                stage.register_index(list(unique.values()), f'{table}({",".join(columns)})',
                                     input_dialect='neug')
            if stage.indexes()['failed']:
                raise ValueError(f'FILL candidates failed: {stage.indexes()["failed"]}')
            plans = OrderedDict()
            for item in stage.build_statements(include_internal_state=True):
                if not item['ok']:
                    raise ValueError(f'{item["index_id"]}: cannot materialize: {item["error"]}')
                plans.setdefault(item['index_id'], []).append(item)
            return stage, plans
        except BaseException:
            stage.close()
            raise

    @staticmethod
    def _check_count_presence(stage, ir):
        # A nullable COUNT can be zero for a nonempty group. The current
        # kernel sometimes uses the visible count as its presence guard,
        # losing that group. Do not activate such a definition until that
        # kernel route has a separate presence proof (not a text heuristic).
        nodes = list(_walk(ir))
        entities = {n['uid']: n for n in nodes if n.get('kind') in ('NODE', 'REL') and 'uid' in n}
        for node in nodes:
            if node.get('k') != 'AGGREGATE_FUNCTION' or node.get('function') != 'COUNT':
                continue
            children = node.get('children') or []
            if len(children) != 1:
                continue
            arg = children[0]
            if 'entity' in arg or arg.get('property') == '_ID':
                continue
            owner = entities.get(arg.get('owner'), {})
            tables = owner.get('tables') or []
            if arg.get('k') == 'PROPERTY' and tables and all(
                    arg.get('property') in (stage.schema.table(t)[1] or {}).get('primary_keys', []) for t in tables):
                continue
            if arg.get('k') == 'LITERAL' and not (arg.get('value') or {}).get('null', True):
                continue
            raise ValueError('COUNT(nullable/computed value): kernel group-presence proof is missing; '
                             'managed catalog refuses to enable this definition')

    def _definitions(self, stage, plans):
        reads, writes, shapes = {}, {}, {}
        child_groups = {}
        for edge in stage.schema.edges:
            for pair in edge.get('vertex_type_pair_relations') or []:
                child_groups[f'{edge["type_name"]}_{pair["source_vertex"]}_{pair["destination_vertex"]}'.upper()] = edge['type_name'].upper()

        def table_name(name):
            return child_groups.get(name.upper(), name.upper())

        for iid, fills in stage.fills.items():
            reads[iid], writes[iid], shapes[iid] = set(), set(), []
            for text in fills:
                fill = parse_fill(text)
                kind, carrier = stage.schema.table(fill.table)
                if carrier is None:
                    # Child carriers need a key precise enough to distinguish
                    # partitions; reject until the file protocol supports it.
                    raise ValueError(f'{iid}: declare the relationship group as the FILL carrier')
                cols = fill.columns or [p['property_name'] for p in carrier.get('properties') or []]
                writes[iid].update((fill.table.upper(), c) for c in cols)
                if not fill.columns:
                    writes[iid].add((fill.table.upper(), '@rows'))
                shapes[iid].append({
                    'kind': kind, 'table': fill.table.upper(), 'columns': cols,
                    'keys': carrier.get('primary_keys'),
                    'pairs': carrier.get('vertex_type_pair_relations'),
                    'types': {c: stage.schema.properties(fill.table)[c] for c in cols},
                    'defaults': {c: _tokens(stage.schema.defaults.get((fill.table.upper(), c), 'NULL')) for c in cols},
                })
                ir = stage.engine.bind(fill.source)['ir']
                nodes = list(_walk(ir))
                entities = {n['uid']: n for n in nodes if n.get('kind') in ('NODE', 'REL') and 'uid' in n}
                for n in nodes:
                    if n.get('kind') == 'REL':
                        reads[iid].update((table_name(t), '@rows') for t in n.get('tables') or [])
                    if n.get('k') == 'PROPERTY' and n.get('property') != '_ID':
                        owner = entities.get(n.get('owner'))
                        if owner is None:
                            raise ValueError(f'{iid}: cannot determine property dependency owner')
                        reads[iid].update((table_name(t), n['property']) for t in owner.get('tables') or [])
        producers = {}
        for iid, columns in writes.items():
            for column in columns:
                if column in producers:
                    raise ValueError(f'Overlapping FILL carriers: {producers[column]} and {iid}: {column}')
                producers[column] = iid
        dependencies = {iid: sorted({producers[c] for c in columns if c in producers})
                        for iid, columns in reads.items()}
        order, visiting, done = [], set(), set()

        def visit(iid):
            if iid in visiting:
                raise ValueError(f'Cyclic FILL dependency involving {iid}')
            if iid in done:
                return
            visiting.add(iid)
            for dep in dependencies[iid]:
                visit(dep)
            visiting.remove(iid)
            done.add(iid)
            order.append(iid)

        for iid in plans:
            visit(iid)
        context = _digest({'base_schema': self.base_schema.doc,
                           'base_defaults': sorted((t, c, v) for (t, c), v in self.base_schema.defaults.items()),
                           'profile': self.mw.profile.name,
                           'mapping': getattr(self.mw.frontend, 'm', {}), 'plan_format': self.VERSION})
        fingerprints = {}
        for iid in order:
            normalized = [{phase: [_tokens(s) for s in item[phase]]
                           for phase in ('ddl', 'reset', 'fill_statements')} for item in plans[iid]]
            fingerprints[iid] = _digest({'context': context, 'carrier': shapes[iid], 'plan': normalized,
                                        'dependencies': {d: fingerprints[d] for d in dependencies[iid]}})
        return order, dependencies, fingerprints

    def _install(self, stage, ready):
        # Compile the selected catalog before replacing the live engine.
        engine = Engine(*self.mw._engine_args)
        try:
            engine.load(stage.schema.to_yaml(), self.mw.stats)
            for iid in ready:
                engine.register(iid, stage.fills[iid])
            if engine.indexes().get('failed_candidates'):
                raise ValueError('Ready index catalog contains failed candidates')
        except BaseException:
            engine.close()
            raise
        old = self.mw.engine
        self.mw.engine = engine
        self.mw.schema = stage.schema.copy()
        self.mw.profile.dialect.set_schema(self.mw.schema)
        if hasattr(self.mw.frontend, 'set_schema'):
            self.mw.frontend.set_schema(self.mw.schema)
        self.mw.fills = OrderedDict((iid, list(stage.fills[iid])) for iid in ready)
        self.mw.failed = []
        self.mw._cache.clear()
        old.close()

    def reload(self, executor=None):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.mw._lock, open(str(self.state_file) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read_state()
            stage, plans = self._stage()
            try:
                order, dependencies, fingerprints = self._definitions(stage, plans)
                previous = state['indexes']
                ready = [iid for iid in order if previous.get(iid, {}).get('status') == 'ready'
                         and previous[iid].get('fingerprint') == fingerprints[iid]]
                pending = [iid for iid in order if iid not in ready]
                report = {'files': list(map(str, self.files)), 'target': self.target,
                          'added': [i for i in order if i not in previous],
                          'changed': [i for i in pending if i in previous],
                          'unchanged': list(ready), 'removed': sorted(set(previous) - set(order)),
                          'pending': list(pending), 'built': [], 'active': list(ready),
                          'dependencies': dependencies, 'fingerprints': fingerprints,
                          'plans': [p for iid in pending for p in plans[iid]]}
                validator = getattr(executor, 'validate', None)
                if validator is not None:
                    validator([s for iid in pending for phase in ('ddl', 'reset', 'fill_statements')
                               for item in plans[iid] for s in item[phase]])
                # A changed definition cannot keep using its old receipt even
                # if a previous build is still physically present.
                self._install(stage, ready)
                self.last_report = report
                if executor is None:
                    return report
                state['indexes'] = {iid: dict(previous.get(iid, {}) if iid in ready else {},
                                             fingerprint=fingerprints[iid],
                                             status='ready' if iid in ready else 'pending',
                                             dependencies=dependencies[iid]) for iid in order}
                self._save(state)
                try:
                    for iid in pending:
                        state['indexes'][iid]['status'] = 'building'
                        self._save(state)
                        # Per-index order matters: finish an upstream index
                        # before resetting/filling any of its consumers.
                        for phase in ('ddl', 'reset', 'fill_statements'):
                            statements = dict.fromkeys(s for item in plans[iid] for s in item[phase])
                            for statement in statements:
                                executor(statement)
                        state['indexes'][iid]['status'] = 'ready'
                        state['indexes'][iid]['built_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        self._save(state)
                        ready.append(iid)
                        report['built'].append(iid)
                        report['pending'].remove(iid)
                finally:
                    self._install(stage, [iid for iid in order if iid in ready])
                    report['active'] = [iid for iid in order if iid in ready]
                    report['plans'] = [p for iid in report['pending'] for p in plans[iid]]
                return report
            finally:
                stage.close()


class Neo4jIndexExecutor:
    """Auto-commit executor and identity of the physical Neo4j database."""

    def __init__(self, uri, database='neo4j', auth=None):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=auth)
        self.database = database
        try:
            with self.driver.session(database=database) as session:
                info = session.run('CALL db.info() YIELD id RETURN id').single()
                if info is None or not info['id']:
                    raise ValueError('Neo4j did not return a physical database id')
                self.target = json.dumps({'uri': uri, 'database': database, 'database_id': info['id']}, sort_keys=True)
        except BaseException:
            self.close()
            raise

    def __call__(self, statement):
        with self.driver.session(database=self.database) as session:
            session.run(statement).consume()

    def validate(self, statements):
        """Compile every pending target statement before the first reset."""
        with self.driver.session(database=self.database) as session:
            for statement in statements:
                session.run('EXPLAIN ' + statement).consume()

    def close(self):
        self.driver.close()
