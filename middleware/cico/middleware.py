"""IncGQ index middleware: Cypher in, Cypher out.

    mw = IndexMiddleware(schema='graph.yaml', profile='kuzu')
    mw.execute("ALTER TABLE PERSON ADD likeCount INT64")          # carrier DDL (optional)
    mw.execute("FILL PERSON(likeCount) FROM (MATCH ... RETURN p.id, count(*))")
    r = mw.execute("MATCH ... RETURN ...", parameters={'tag': 'Rumi'})
    r.cypher, r.rewritten, r.reason, r.indexes

Indexes are written into the middleware at runtime with FILL statements; a
missing FILL target column (or relationship table) is inferred from the
source query and added to the middleware's schema.  Every rewrite is printed
for the target dialect and verified by re-binding; on any doubt the original
query is returned unchanged.
"""
import json
import datetime
import re
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .canonical import canonical, first_difference, raw_names, rel_pairs_of
from .engine import Engine, EngineError
from .errors import Unsupported
from .printer import Printer
from .profiles import get_profile
from .schema import Schema, SchemaError
from . import queryparts
from .statements import classify, parse_fill, split_script, strip_comments


@dataclass
class Result:
    kind: str                       # query | fill | ddl | noop | error
    ok: bool = True
    cypher: str = ''                # statement for the target system
    original: str = ''
    rewritten: bool = False
    reason: str = ''
    detail: str = ''
    indexes: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    index_id: str = ''
    entries: list = field(default_factory=list)
    inferred_ddl: list = field(default_factory=list)
    schema_changed: bool = False
    route: str = ''
    specialized: list = field(default_factory=list)   # parameters inlined as literals
    parts: list = field(default_factory=list)         # rewritten QueryPart numbers (1-based)
    proofs: list = field(default_factory=list)        # checked local semantic rules (not Lean certificates)

    def to_dict(self):
        return asdict(self)


def internal_literal(value):
    """A parameter value as an internal (NeuG) Cypher literal."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in '.eEn') else text + '.0'
    if isinstance(value, str):
        return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
    if isinstance(value, datetime.datetime):
        from .normalize import timestamp_text
        if value.tzinfo is not None:
            # An aware value denotes an instant; internal TIMESTAMPs are UTC.
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return f"TIMESTAMP('{timestamp_text(value)}')"
    if isinstance(value, datetime.date):
        return f"DATE('{value.isoformat()}')"
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(internal_literal(v) for v in value) + ']'
    raise ValueError(f'cannot inline parameter value {value!r}')


def inline_parameters(internal, values):
    """Replace `$name` references (outside strings) by literals."""
    from .neo4j import render, tokenize
    tokens = tokenize(internal)
    for token in tokens:
        if token.kind == 'param':
            name = token.text[1:].strip('`')
            if name in values:
                token.text = internal_literal(values[name])
                token.kind = 'literal'
    return render(tokens)


def infer_parameter_types(parameters):
    types = {}
    for name, value in (parameters or {}).items():
        if isinstance(value, bool):
            types[name] = 'BOOL'
        elif isinstance(value, int):
            types[name] = 'INT64'
        elif isinstance(value, float):
            types[name] = 'DOUBLE'
        elif isinstance(value, str):
            types[name] = 'STRING'
        elif isinstance(value, datetime.datetime):
            types[name] = 'TIMESTAMP'
        elif isinstance(value, datetime.date):
            types[name] = 'DATE'
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            types[name] = 'STRING[]'
        elif isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            types[name] = 'INT64[]'
        else:
            types[name] = 'ANY'
    return types


NEUG_REJECTS = re.compile(r'^\s*(Not supported|Unsupported|Parser exception|Catalog exception: function'
                          r'|Binder exception: Cannot match a built-in function)')


def neug_rejects(error):
    """NeuG's own verdict that a construct is outside its language (parser,
    function catalog, missing overload, explicitly unsupported feature): the
    QueryPart cannot be converted.  Internal errors (assertions, crashes) are
    not matched here; they are conversion bugs to fix."""
    return bool(NEUG_REJECTS.match(str(error.response.get('error', ''))))


def _used(types, text):
    """Parameter types restricted to the parameters `text` references (the
    binder rejects declared parameters a statement does not use)."""
    from .neo4j import tokenize
    from .syntax import parameter_names
    names = parameter_names(tokenize(text))
    return {k: v for k, v in types.items() if k in names}


def _columns(columns):
    """Un-aliased output columns are named after an internal unique name
    (`_6_person.id`); compare them without the binder's numeric prefix."""
    return [[re.sub(r'^_\d+_', '', name), type_name] for name, type_name in columns or []]


class IndexMiddleware:
    def __init__(self, schema, stats=None, profile='neug', verify=True, engine_binary=None,
                 engine_log=None, cache_size=4096, specialize=True, scope='part'):
        self.specialize = specialize
        # 'part': rewrite each QueryPart on its own (the wiki's QueryPart
        # theorem); 'query': hand the whole statement to RewriterV4 (M1 mode).
        if scope not in ('part', 'query'):
            raise ValueError(f'scope must be part or query, not {scope!r}')
        self.scope = scope
        self.schema = schema if isinstance(schema, Schema) else Schema.from_file(schema)
        self.stats = Path(stats).read_text() if stats else None
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.printer = Printer(self.profile.dialect)
        self.profile.dialect.set_schema(self.schema)
        if hasattr(self.profile.frontend, 'set_schema'):
            self.profile.frontend.set_schema(self.schema)
        self.frontend = self.profile.frontend
        self.verify = verify
        self._engine_args = (engine_binary, engine_log)
        self.engine_restarts = 0
        self.engine = Engine(engine_binary, engine_log)
        self.failed = self.engine.load(self.schema.to_yaml(), self.stats).get('failed_candidates', [])
        self.fills = OrderedDict()      # index id -> internal FILL texts
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.RLock()

    def close(self):
        self.engine.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --------------------------------------------------------------- API
    def execute(self, statement, parameters=None, parameter_types=None, index_id=None):
        kind = classify(statement)
        with self._lock:
            try:
                if self.frontend.name == 'neo4j' and re.match(
                        r'^\s*(?:CREATE\s+(?:(?:RANGE|TEXT|POINT|FULLTEXT|VECTOR|LOOKUP)\s+)?(?:INDEX|CONSTRAINT)|DROP\s+(?:INDEX|CONSTRAINT))\b',
                        statement, re.I):
                    return Result('ddl', cypher=statement, original=statement, reason='target_ddl', route='target')
                if kind == 'fill':
                    return self.register_fill(statement, index_id)
                if kind == 'ddl':
                    return self.apply_ddl(statement)
                if kind == 'noop':
                    return Result('noop', cypher=statement, original=statement, reason='ignored')
                return self.rewrite(statement, parameters, parameter_types)
            except (SchemaError, EngineError, Unsupported, ValueError) as error:
                return Result('error', ok=False, cypher=statement, original=statement,
                              reason=type(error).__name__, detail=str(error))
            finally:
                if not self.engine.alive():
                    self._restart_engine()

    def _restart_engine(self):
        """The engine died during a request (e.g. its memory cap, see
        engine._limit_memory): start a new one with the same schema and FILLs.
        The failed request has already returned its input unchanged."""
        self.engine.close()
        self.engine = Engine(*self._engine_args)
        self.engine_restarts += 1
        self.failed = self.engine.load(self.schema.to_yaml(), self.stats).get('failed_candidates', [])
        for index_id, texts in self.fills.items():
            self.engine.register(index_id, texts)

    def execute_script(self, text):
        return [self.execute(s) for s in split_script(text)]

    def register_neo4j_index(self, script, index_id=None, parameters=None):
        """Register Neo4j MATCH/SET and derived MERGE materializations.

        The supplied script describes already built (or caller-built) indexes.
        This method compiles and registers metadata only; it never executes SQL.
        All definitions must translate and bind, otherwise registration rolls back.
        """
        from .neo4j_indexes import definitions
        if self.frontend.name != 'neo4j':
            raise ValueError('Neo4j index scripts require the neo4j profile')
        with self._lock:
            previous_schema = self.schema.copy()
            previous_fills = self.fills.copy()
            try:
                defaults = {}
                groups = definitions(script, self.frontend, self.schema, parameters, defaults)
                # Infer all endpoint partitions together before the first
                # branch creates a relationship carrier (e.g. Post->Post and
                # Comment->Post in ROOT_POST).
                candidate = self.schema.copy()
                inferred_by_group = [[] for _ in groups]
                for group_number, fills in enumerate(groups):
                    first = parse_fill(fills[0])
                    if not first.columns and candidate.table(first.table)[0] is None:
                        pairs = set()
                        for text in fills:
                            bound = self.engine.bind(parse_fill(text).source)
                            src = self._owner_tables(bound['ir'], 0)
                            dst = self._owner_tables(bound['ir'], 1)
                            if not src or not dst:
                                raise SchemaError('Cannot infer Neo4j derived edge endpoints')
                            pairs.update((s, d) for s in src for d in dst)
                        candidate.create_rel_table(first.table, sorted(pairs), [])
                        body = ', '.join(f'FROM {src} TO {dst}' for src, dst in sorted(pairs))
                        inferred_by_group[group_number].append(f'CREATE REL TABLE {first.table}({body})')
                for group_number, fills in enumerate(groups):
                    for text in fills:
                        inferred_by_group[group_number].extend(self._infer_carriers(parse_fill(text), candidate))
                candidate.defaults.update(defaults)
                if candidate.to_yaml() != self.schema.to_yaml() or defaults:
                    self._reload(candidate)
                results = []
                for i, fills in enumerate(groups):
                    name = (index_id if len(groups) == 1 else f'{index_id}:{i + 1}') if index_id else None
                    result = self.register_index(fills, name)
                    result.inferred_ddl = inferred_by_group[i] + result.inferred_ddl
                    result.schema_changed = result.schema_changed or bool(inferred_by_group[i])
                    results.append(result)
                return results
            except Exception:
                self.fills = previous_fills
                self._reload(previous_schema)
                self._restart_engine()
                raise

    def indexes(self):
        response = self.engine.indexes()
        return dict(indexes=response['indexes'], failed=response.get('failed_candidates', []),
                    fills=dict(self.fills))

    def drop_index(self, index_id):
        with self._lock:
            dropped = self.engine.drop(index_id)['dropped']
            self.fills.pop(index_id, None)
            self._cache.clear()
            return dropped

    # --------------------------------------------------------------- DDL
    def apply_ddl(self, statement, *, input_dialect=None):
        if input_dialect not in (None, 'neug'):
            raise ValueError('input_dialect must be None (profile) or neug')
        internal = statement if input_dialect == 'neug' else self.frontend.ddl_to_internal(statement)
        candidate = self.schema.copy()
        changed = candidate.apply_ddl(internal)
        if changed:
            self._reload(candidate)
        return Result('ddl', cypher=statement, original=statement, schema_changed=changed,
                      reason='applied' if changed else 'no_change')

    def _reload(self, schema):
        response = self.engine.load(schema.to_yaml(), self.stats)
        self.schema = schema
        self.profile.dialect.set_schema(schema)
        if hasattr(self.profile.frontend, 'set_schema'):
            self.profile.frontend.set_schema(schema)
        self.failed = response.get('failed_candidates', [])
        self._cache.clear()
        return response

    # -------------------------------------------------------------- FILL
    def register_fill(self, statement, index_id=None):
        """Register one FILL statement as an index (id defaults to TABLE(cols))."""
        return self.register_index([statement], index_id)

    def register_index(self, fills, index_id=None, *, input_dialect=None):
        """Register an index made of one or more FILL branches, all or none.
        Missing carrier columns / relationship tables are inferred from the
        FILL source queries and added to the middleware schema."""
        if input_dialect not in (None, 'neug'):
            raise ValueError('input_dialect must be None (profile) or neug')
        parsed = []
        for statement in fills:
            try:
                internal = statement if input_dialect == 'neug' else self.frontend.fill_to_internal(statement)
            except Unsupported:
                # Index definitions may also be written in the internal vocabulary.
                internal = statement
            fill = parse_fill(internal)
            if fill is None:
                raise ValueError(f'Cannot parse FILL statement: {statement[:120]}')
            parsed.append(fill)
        index_id = index_id or self._default_id(parsed[0])
        texts = [f.text for f in parsed]
        for existing_id, existing in self.fills.items():
            if existing == texts:  # the same definition again (shared index scripts)
                return Result('fill', cypher='; '.join(fills), original='; '.join(fills),
                              index_id=existing_id, reason='already_registered')
        if index_id in self.fills:
            raise ValueError(f'Index {index_id} already exists; drop it first')
        candidate = self.schema.copy()
        inferred = []
        for fill in parsed:
            inferred += self._infer_carriers(fill, candidate)
        previous = self.schema
        if inferred:
            self._reload(candidate)
        try:
            response = self.engine.register(index_id, [f.text for f in parsed])
        except EngineError:
            if inferred:
                self._reload(previous)
            raise
        self.fills[index_id] = [f.text for f in parsed]
        self._cache.clear()
        return Result('fill', cypher='; '.join(fills), original='; '.join(fills), index_id=index_id,
                      entries=response['entries'], inferred_ddl=inferred, schema_changed=bool(inferred),
                      reason='registered')

    def _default_id(self, fill):
        base = f"{fill.table}({','.join(fill.columns)})"
        index_id, n = base, 1
        while index_id in self.fills:
            n += 1
            index_id = f'{base}#{n}'
        return index_id

    def _infer_carriers(self, fill, schema):
        """Add missing FILL target columns / relationship table to `schema`.
        Returns the DDL statements (internal dialect) that were inferred."""
        described = self.engine.bind(fill.source)
        outputs = described['columns']
        kind, _ = schema.table(fill.table)
        ddl = []
        if kind is None and self._is_group_child(schema, fill.table):
            return ddl  # one child (GROUP_SRC_DST) of an existing REL group
        if kind is None:
            keys = 2
            if len(outputs) != keys + len(fill.columns):
                raise SchemaError(f'Unknown FILL target table {fill.table}')
            src, dst = (self._owner_tables(described['ir'], i) for i in (0, 1))
            if not src or not dst:
                raise SchemaError(f'Cannot infer endpoints of relationship table {fill.table}')
            pairs = [(s, d) for s in src for d in dst]
            columns = [(c, outputs[keys + i][1]) for i, c in enumerate(fill.columns)]
            schema.create_rel_table(fill.table, pairs, columns)
            body = ', '.join([f'FROM {s} TO {d}' for s, d in pairs] + [f'{c} {t}' for c, t in columns])
            return [f'CREATE REL TABLE {fill.table}({body})']
        keys = 1 if kind == 'NODE' else 2
        if fill.columns and len(outputs) != keys + len(fill.columns):
            raise SchemaError(f'FILL {fill.table}: source returns {len(outputs)} columns, '
                              f'expected {keys} key(s) + {len(fill.columns)} value(s)')
        for i, column in enumerate(fill.columns):
            if not schema.has_property(fill.table, column):
                type_name = outputs[keys + i][1]
                schema.add_property(fill.table, column, type_name)
                ddl.append(f'ALTER TABLE {fill.table} ADD {column} {type_name}')
        return ddl

    @staticmethod
    def _is_group_child(schema, name):
        for edge in schema.edges:
            for pair in edge.get('vertex_type_pair_relations') or []:
                if f"{edge['type_name']}_{pair['source_vertex']}_{pair['destination_vertex']}".upper() == name.upper():
                    return True
        return False

    @staticmethod
    def _owner_tables(ir, position):
        try:
            part = ir['single_queries'][0]['parts'][-1]
            expression = part['projection']['expressions'][position]
        except (KeyError, IndexError, TypeError):
            return None
        owner = expression.get('owner')
        found = []

        def walk(value):
            if isinstance(value, dict):
                if value.get('uid') == owner and value.get('kind') == 'NODE':
                    found.extend(value.get('tables') or [])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(ir)
        return list(dict.fromkeys(found))

    # ------------------------------------------------------ materialization
    def build_statements(self, index_id=None, *, include_internal_state=False):
        """Target-dialect statements that materialize registered indexes:
        [{'index_id', 'fill', 'ddl': [...], 'reset': [...], 'fill_statements': [...]}].
        Run every ddl, then every reset, then every fill statement."""
        from .materialize import Materializer
        m = Materializer(self, include_internal_state=include_internal_state)
        out = []
        for iid, fills in self.fills.items():
            if index_id is not None and iid != index_id:
                continue
            for fill in fills:
                try:
                    built = m.statements(fill)
                    out.append(dict(index_id=iid, fill=fill, ok=True, ddl=built['ddl'],
                                    reset=built['reset'], fill_statements=built['fill']))
                except (Unsupported, EngineError) as error:
                    out.append(dict(index_id=iid, fill=fill, ok=False, error=str(error),
                                    ddl=[], reset=[], fill_statements=[]))
        return out

    # ------------------------------------------------------------ rewrite
    def rewrite(self, cypher, parameters=None, parameter_types=None):
        if parameter_types is None:
            parameter_types = infer_parameter_types(parameters)
        parameter_types = self.frontend.parameter_types(parameter_types)
        key = (cypher, json.dumps(parameter_types, sort_keys=True))
        result = self._cached(key, lambda: self._rewrite_query(cypher, parameter_types))
        retry = result.reason == 'no_index_selected' or result.reason.startswith('engine_error') \
            or 'needs literal' in (result.detail or '')
        if result.rewritten or not retry or not parameters or not self.specialize:
            return result
        # FILLs are often built for fixed parameter values (time windows,
        # thresholds), and some parameter uses only bind as literals (string
        # concatenation in duration(), which the internal binder lacks).
        # Retry with the given values inlined; the result is then only valid
        # for these values, so it is cached by value.
        values = {k: v for k, v in parameters.items() if k in parameter_types}
        key = key + (json.dumps(values, sort_keys=True, default=str),)
        return self._cached(key, lambda: self._specialize(cypher, parameter_types, values) or result)

    def _cached(self, key, compute):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        result = compute()
        self._cache[key] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    def _specialize(self, cypher, types, values):
        try:
            names = [n for n in values if internal_literal(values[n]) is not None]
        except ValueError:
            return None
        attempt = lambda inlined: self._rewrite_query(
            cypher, {k: t for k, t in types.items() if k not in inlined},
            {k: values[k] for k in inlined})
        best = attempt(names)
        if not best.rewritten:
            return None
        inlined = list(names)
        for name in names:  # keep every parameter the rewrite does not need
            trial = [n for n in inlined if n != name]
            result = attempt(trial)
            if result.rewritten and result.indexes == best.indexes:
                inlined, best = trial, result
        best.specialized = inlined
        best.detail = (f'valid only for the given values of {", ".join("$" + n for n in inlined)}'
                       if inlined else best.detail)
        return best

    # ------------------------------------------------------- QueryPart scope
    REASON_RANK = ('rewritten', 'verification_mismatch', 'verification_bind_failed',
                   'verification_frontend_failed', 'part_context_changed', 'unsupported', 'printer_error',
                   'no_index_selected', 'not_read_only')

    def _rewrite_query(self, cypher, types, inline=None):
        """Rewrite every QueryPart separately (see queryparts.py) and splice
        the rewritten parts into the original text."""
        from .neo4j import tokenize
        tokens = tokenize(strip_comments(cypher))
        if any(t.kind == 'ident' and t.upper in ('CREATE', 'MERGE', 'SET', 'DELETE', 'DETACH', 'REMOVE', 'FOREACH', 'LOAD')
               and (i == 0 or tokens[i - 1].text != '.') for i, t in enumerate(tokens)):
            return self._fallback(cypher, 'not_read_only')
        if self.scope == 'query':
            return self._rewrite(cypher, types, inline)
        # CALL remains opaque to the internal frontend. Compile its read-only
        # branches with the imported binding, and splice only verified bodies.
        from .subqueries import rewrite_calls
        call_text, call_indexes, call_notes, call_proofs = rewrite_calls(self, strip_comments(cypher), types, inline)
        source = call_text or cypher
        from .scalar_subqueries import rewrite_scalar_regions
        scalar_text, scalar_indexes, proofs, scalar_notes = rewrite_scalar_regions(
            self, strip_comments(source), types, inline)
        source = scalar_text or source
        # Relationship-carrier existence parts (edge_carriers.py): a separate
        # local rule, used only where RewriterV4 leaves the part unchanged.
        from .edge_carriers import rewrite_parts as carrier_parts
        try:
            overrides, carrier_notes = carrier_parts(self, strip_comments(source), types, inline)
        except (Unsupported, EngineError) as error:
            overrides, carrier_notes = {}, ['carrier existence: ' + str(error)[:200]]
        result = self._rewrite_parts(source, types, inline, overrides)
        if carrier_notes and not result.rewritten:
            result.detail = ' | '.join(filter(None, [result.detail, ' | '.join(carrier_notes)]))
        if call_notes:
            result.detail = ' | '.join(filter(None, [result.detail, 'CALL: ' + ' | '.join(call_notes)]))
        if scalar_notes:
            result.detail = ' | '.join(filter(None, [result.detail, 'expressions: ' + ' | '.join(scalar_notes)]))
        if call_text or scalar_text:
            result.original = cypher
            result.rewritten = True
            result.reason = 'rewritten'
            result.indexes = sorted(set(result.indexes) | set(call_indexes) | set(scalar_indexes))
            result.proofs.extend(call_proofs + proofs)
            routes = (['subqueries'] if call_text else []) + (['expressions'] if scalar_text else [])
            result.route = ':'.join(routes + ([result.route] if result.route else []))
        return result

    def _bind_prefix(self, source, types, inline=None):
        """Bind the actual input schema; opaque type probes never reach output."""
        def bind_columns(text):
            internal = self._to_internal(text, inline)
            return self.engine.bind(internal, _used(types, internal))['columns']
        try:
            internal = self._to_internal(source, inline)
            return self.engine.bind(internal, _used(types, internal))
        except (Unsupported, EngineError) as error:
            if self.frontend.name != 'neo4j' or isinstance(error, EngineError) and not neug_rejects(error):
                raise
        typed = queryparts.type_probe_prefix(source, self.frontend, self.profile.dialect, bind_columns)
        internal = self._to_internal(typed, inline)
        return self.engine.bind(internal, _used(types, internal))

    def _rewrite_parts(self, cypher, types, inline=None, overrides=None):
        parts = queryparts.split_parts(strip_comments(cypher))
        if parts is None or len(parts) == 1:
            return self._rewrite(cypher, types, inline)
        new, notes, indexes, rewritten, results, proofs = list(parts), [], set(), [], [], []
        for i, part in enumerate(parts):
            if not queryparts.has_match(part):
                continue
            result = self._rewrite_part(parts, i, types, inline)
            results.append(result)
            if not result.rewritten and overrides and i in overrides:
                text, used, proof = overrides[i]
                new[i] = text
                indexes.update(used)
                proofs.append(proof)
                rewritten.append(i + 1)
                continue
            if not result.rewritten:
                notes.append(f'part {i + 1}: {result.reason} {result.detail[:200]}'.strip())
                continue
            new[i] = result.part_text
            indexes.update(result.indexes)
            rewritten.append(i + 1)
        if not rewritten:
            best = min(results, key=self._rank) if results else None
            reason = best.reason if best else 'no_index_selected'
            return self._fallback(cypher, reason, ' | '.join(notes), columns=best.columns if best else [])
        text = ' '.join(new)
        return Result('query', cypher=text, original=cypher, rewritten=True, reason='rewritten',
                      detail=' | '.join(notes), indexes=sorted(indexes), parts=rewritten,
                      route='parts:' + ','.join(map(str, rewritten)), proofs=proofs)

    def _rank(self, result):
        reason = result.reason.split(':')[0]
        return self.REASON_RANK.index(reason) if reason in self.REASON_RANK else len(self.REASON_RANK)

    def _rewrite_part(self, parts, i, types, inline):
        """Probe = stub(S_{i-1}) + part i + RETURN outputs; keep part i only."""
        from .neo4j import tokenize
        from .syntax import parameter_names
        last = i == len(parts) - 1
        outputs = None if last else queryparts.projection_outputs(parts[i])
        if not last and outputs is None:
            return self._fallback(parts[i], 'unsupported', 'projection outputs')
        stub, stub_types = '', {}
        if i > 0:
            names = queryparts.projection_outputs(parts[i - 1])
            prefix = ' '.join(parts[:i]) + ' RETURN ' + ', '.join(names or [])
            try:
                if not names:
                    raise Unsupported('projection outputs of the previous part')
                try:
                    columns = self._bind_prefix(prefix, types, inline)['columns']
                except Unsupported as error:
                    raise Unsupported(f'an earlier QueryPart is not convertible: {error}') from None
                except EngineError as error:
                    if not neug_rejects(error):
                        raise
                    raise Unsupported('an earlier QueryPart is not convertible: NeuG: '
                                      + error.response.get('error', '')) from None
                reserved = set(types) | set(inline or {}) | parameter_names(tokenize(parts[i]))
                stub, stub_types = queryparts.stub(columns, self.profile.dialect, names, reserved)
            except (Unsupported, EngineError) as error:
                detail = error.response.get('error', '') if isinstance(error, EngineError) else str(error)
                if isinstance(error, Unsupported) and 'not convertible' not in detail:
                    detail = f'input of this QueryPart cannot be stubbed: {detail}'
                return self._fallback(parts[i], 'part_input_unavailable', detail[:300])
        tail = '' if last else ' RETURN ' + ', '.join(outputs)
        probe = (stub + ' ' if stub else '') + parts[i] + tail
        probe_types = _used({**types, **stub_types}, probe)
        result = self._rewrite(probe, probe_types, inline)
        if not result.rewritten:
            return result
        printed = getattr(result, 'printed_parts', [])
        head, end = (1 if stub else 0), (0 if last else 1)
        if len(printed) <= head + end:
            return self._fallback(parts[i], 'part_context_changed', 'rewritten probe lost its part')
        # The stub and the trailing RETURN are context, not part of the rewrite:
        # they must print exactly as the probe without indexes does.
        try:
            internal = self._to_internal(probe, inline)
            baseline = self.printer.print(self.engine.bind(internal, probe_types)['ir']).parts
        except (Unsupported, EngineError, KeyError, TypeError, IndexError) as error:
            return self._fallback(parts[i], 'part_context_changed', f'baseline probe: {error}'[:300])
        lead = ''
        if head and printed[0] != baseline[0]:
            # A pattern fully covered by the index and anchored on input
            # variables leaves only a filter on the input rows (e.g. the
            # existence test `x.iv_count <> 0`); RewriterV4 attaches it to the
            # stub's WITH.  It filters part i's input, so it moves into part i.
            extra = printed[0][len(baseline[0]):] if printed[0].startswith(baseline[0]) else None
            if not extra or not extra.startswith(' WHERE ') or ' WHERE ' in baseline[0].split(' WITH ')[-1]:
                return self._fallback(parts[i], 'part_context_changed', f'stub changed: {printed[0]}')
            d = self.profile.dialect
            lead = 'WITH ' + ', '.join(d.ident(n) for n in queryparts.projection_outputs(parts[i - 1])) + extra
        if end and printed[-1] != baseline[-1]:
            return self._fallback(parts[i], 'part_context_changed', f'tail changed: {printed[-1]}')
        result.part_text = ' '.join(([lead] if lead else []) + printed[head:len(printed) - end])
        if parameter_names(tokenize(result.part_text)) & set(stub_types):
            return self._fallback(parts[i], 'part_context_changed', 'stub parameter in the rewritten part')
        return result

    def _to_internal(self, cypher, inline=None):
        """Dialect conversion: target Cypher -> internal Cypher that NeuG can
        bind, or Unsupported.  Parameters to specialize are inlined (and
        constant string concatenations folded) before the NeuG surface check,
        so e.g. duration({hours: $delta}) converts once $delta is a literal."""
        from .normalize import fold_constants, normalize_temporal_comparisons
        from .surface import check
        internal = self.frontend.to_internal(cypher)
        if inline:
            internal = inline_parameters(internal, inline)
        internal = normalize_temporal_comparisons(fold_constants(internal))
        check(internal, self.schema)
        return internal

    def _fallback(self, cypher, reason, detail='', **extra):
        return Result('query', cypher=cypher, original=cypher, reason=reason, detail=detail, **extra)

    def _rewrite(self, cypher, types, inline=None):
        try:
            internal = self._to_internal(cypher, inline)
        except Unsupported as error:
            return self._fallback(cypher, 'frontend_unsupported', str(error))
        try:
            response = self.engine.rewrite(internal, types)
        except EngineError as error:
            if 'unordered_map::at' in str(error) and not getattr(self, '_isolating', False):
                # Some overlapping aggregate candidates fail inside NeuG's
                # combined catalog. Retry each catalog independently, keeping
                # the same printer and verification guard.
                original_engine = self.engine
                self._isolating = True
                try:
                    for name, fills in self.fills.items():
                        isolated = Engine(*self._engine_args)
                        try:
                            isolated.load(self.schema.to_yaml(), self.stats)
                            isolated.register(name, fills)
                            self.engine = isolated
                            result = self._rewrite(cypher, types, inline)
                            if result.rewritten:
                                result.route = 'isolated:' + name
                                return result
                        except EngineError:
                            pass
                        finally:
                            self.engine = original_engine
                            isolated.close()
                finally:
                    self._isolating = False
            if neug_rejects(error):
                return self._fallback(cypher, 'frontend_unsupported', 'NeuG: ' + error.response.get('error', '')[:300])
            return self._fallback(cypher, f'engine_error:{error.stage}', error.response.get('error', ''))
        common = dict(columns=response.get('columns', []))
        if not response.get('read_only'):
            return self._fallback(cypher, 'not_read_only', **common)
        if not response.get('changed'):
            return self._fallback(cypher, 'no_index_selected', **common)
        used = self._carriers_read(response['ir'])
        common.update(indexes=used, route=response.get('route', ''))
        if not used:
            # RewriterV4 also normalises statements; without an index read a
            # re-printed query has no benefit, so keep the user's text.
            return self._fallback(cypher, 'no_index_selected', 'statement changed without reading an index',
                                  **common)
        try:
            printed = self.printer.print(response['ir'])
        except Unsupported as error:
            return self._fallback(cypher, 'unsupported', str(error), **common)
        except (KeyError, TypeError, IndexError) as error:
            return self._fallback(cypher, 'printer_error', repr(error), **common)
        if self.verify:
            problem = self._verify(printed, response['ir'], types, response.get('columns'))
            if problem:
                return self._fallback(cypher, problem[0], f'{problem[1]} | printed: {printed.cypher}', **common)
        result = Result('query', cypher=printed.cypher, original=cypher, rewritten=True, reason='rewritten', **common)
        result.printed_parts = printed.parts
        return result

    def _carriers_read(self, ir):
        """Index candidates whose physical carriers (FILL target columns or
        derived relationship tables) the selected statement reads."""
        columns, relations = {}, {}
        for entry in self.engine.indexes()['indexes']:
            table = entry['target_table'].upper()
            if entry['target_columns']:
                for column in entry['target_columns']:
                    columns.setdefault((table, column), set()).add(entry['candidate'])
            else:
                relations.setdefault(table, set()).add(entry['candidate'])
        entity_tables, used = {}, set()

        def collect(value):
            if isinstance(value, dict):
                if value.get('kind') in ('NODE', 'REL') and 'tables' in value:
                    groups = {self.profile.dialect.rel_group.get(t, t).upper() for t in value['tables']}
                    entity_tables.setdefault(value['uid'], set()).update(groups)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        def scan(value):
            if isinstance(value, dict):
                if value.get('k') == 'PROPERTY':
                    for table in entity_tables.get(value.get('owner'), ()):
                        used.update(columns.get((table, value.get('property')), ()))
                if value.get('kind') == 'REL':
                    for table in entity_tables.get(value['uid'], ()):
                        used.update(relations.get(table, ()))
                for child in value.values():
                    scan(child)
            elif isinstance(value, list):
                for child in value:
                    scan(child)
        collect(ir)
        scan(ir)
        return sorted(used)

    def _verify(self, printed, selected, types, columns):
        try:
            internal = self._to_internal(printed.cypher)
            rebound = self.engine.bind(internal, types)
        except Unsupported as error:
            return 'verification_frontend_failed', str(error)
        except EngineError as error:
            return 'verification_bind_failed', error.response.get('error', '')
        pairs = rel_pairs_of(self.schema)
        expected = canonical(selected, printed.entity_names, pairs)
        actual = canonical(rebound['ir'], raw_names(rebound['ir']), pairs)
        difference = first_difference(expected, actual)
        if difference:
            return 'verification_mismatch', difference
        if _columns(rebound.get('columns')) != _columns(columns):
            return 'verification_mismatch', f"columns {rebound.get('columns')} != {columns}"
        return None


# Backwards-compatible name used by the M1 tests and docs.
IndexRewriter = IndexMiddleware
