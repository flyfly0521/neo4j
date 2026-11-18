"""Neo4j profile: schema mapping + Cypher translation in both directions.

Neo4jFrontend   Neo4j Cypher  -> internal (NeuG) Cypher, for binding/rewriting
Neo4jDialect    Bound IR      -> Neo4j Cypher, for output

Both are driven by a mapping file (profiles/ldbc-snb-neo4j.yaml).  The
translation is token based and pattern aware; anything it cannot translate
faithfully raises Unsupported, so the middleware returns the original query.
Output is verified by translating it back and re-binding.
"""
import re
from pathlib import Path

import yaml

from .dialects.base import Dialect
from .errors import Unsupported
from .statements import strip_comments

UNBOUNDED = 4294967295
TEMPORAL_FIELDS = {'year', 'month', 'day', 'hour', 'minute', 'second', 'millisecond'}
TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<quoted>`(?:[^`]|``)*`)
  | (?P<param>\$(?:[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|\d+))
  | (?P<number>\d+\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct><-|->|<=|>=|<>|=~|\.\.|.)
""", re.X | re.S)
PATTERN_KEYWORDS = {'MATCH', 'MERGE', 'CREATE'}
CLAUSE_END = {'WHERE', 'WITH', 'RETURN', 'UNWIND', 'ORDER', 'SET', 'DELETE', 'DETACH', 'UNION',
              'LIMIT', 'SKIP', 'CALL', 'OPTIONAL', 'MATCH', 'REMOVE', 'FOREACH', 'MERGE', 'CREATE'}


def closing_index(tokens, opening):
    from .syntax import closing
    return closing(tokens, opening)


def load_mapping(path):
    with open(path) as handle:
        mapping = yaml.safe_load(handle)
    mapping.setdefault('labels', {})
    mapping.setdefault('typed_labels', {})
    mapping.setdefault('rel_types', {})
    mapping.setdefault('acyclic_rel_types', [])  # variable-length walks == trails on these
    mapping.setdefault('constraints', [])        # declared integrity constraints (constraints.py)
    mapping.setdefault('properties', {})         # internal table -> {Neo4j property: internal property}
    return mapping


class Tok:
    __slots__ = ('kind', 'text', 'ws')

    def __init__(self, kind, text, ws=''):
        self.kind, self.text, self.ws = kind, text, ws

    @property
    def upper(self):
        return self.text.upper()

    def __repr__(self):
        return f'{self.kind}:{self.text}'


def tokenize(text):
    tokens, ws = [], ''
    for m in TOKEN.finditer(text):
        kind = m.lastgroup
        if kind == 'ws':
            ws += m.group()
            continue
        tokens.append(Tok(kind, m.group(), ws))
        ws = ''
    return tokens


def render(tokens):
    return ''.join(t.ws + t.text for t in tokens).strip()


def _ident(tok):
    return tok.text[1:-1].replace('``', '`') if tok.kind == 'quoted' else tok.text


def _q(name):
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) else '`' + name.replace('`', '``') + '`'


class Neo4jFrontend:
    """Neo4j Cypher -> internal Cypher."""
    name = 'neo4j'

    def __init__(self, mapping):
        self.m = mapping
        self.labels = {k: v for k, v in mapping['labels'].items()}
        self.typed = mapping['typed_labels']
        self.rels = mapping['rel_types']
        self.procedure_types = {name.lower(): contract for name, contract in
                                mapping.get('procedure_types', {}).items()}
        self.acyclic = set(mapping['acyclic_rel_types'])
        from .constraints import Constraints
        typed = {k: [v['table']] for k, v in mapping['typed_labels'].items()}
        self.constraints = Constraints(mapping['constraints'], {**mapping['labels'], **typed},
                                       typed=mapping['typed_labels'].keys())
        self.relax = False      # apply constraint-justified simplifications (retry stage)
        self.premises = set()   # constraints the last translations relied on
        self.prop_names = {t.upper(): dict(m) for t, m in mapping['properties'].items()}
        self.rel_pairs = {}     # internal relationship table -> {(src, dst)}
        self.node_names = set()
        self.node_case = {}
        self.var_tables = {}    # per statement: variable -> node tables

    def set_schema(self, schema):
        self.node_names = {v['type_name'].upper() for v in schema.vertices}
        self.node_case = {v['type_name'].upper(): v['type_name'] for v in schema.vertices}
        self.rel_pairs = {}
        for edge in schema.edges:
            pairs = {(p['source_vertex'].upper(), p['destination_vertex'].upper())
                     for p in edge.get('vertex_type_pair_relations') or []}
            self.rel_pairs[edge['type_name'].upper()] = pairs

    # ----------------------------------------------------------- public API
    def to_internal(self, cypher):
        tokens = tokenize(strip_comments(cypher).strip().rstrip(';'))
        tokens = self._count_relationships(tokens)
        tokens = self._drop_hints(tokens)
        tokens = self._shortest_paths(tokens)
        if self.relax:
            tokens = self._implied_typed_labels(tokens)
        self.var_tables = self._collect_var_tables(tokens)
        from .normalize import entity_id_comparisons, rewrite_coalesce, split_chained_comparisons
        tokens = split_chained_comparisons(rewrite_coalesce(tokens))
        tokens = self._with_uniqueness(tokens)
        multi = {t for t, pairs in self.rel_pairs.items() if len(pairs) > 1}
        return render(entity_id_comparisons(tokenize(render(self._translate(tokens))), multi))

    @staticmethod
    def _count_relationships(tokens):
        # In a successful match a relationship and its fresh anonymous end
        # node are both non-null; in an unmatched OPTIONAL they are both null.
        # COUNT (without DISTINCT) therefore has identical multiplicity.
        counts = {tokens[i + 2].text for i, t in enumerate(tokens[:-3])
                  if t.upper == 'COUNT' and tokens[i + 1].text == '('
                  and tokens[i + 2].kind in ('ident', 'quoted') and tokens[i + 3].text == ')'}
        edits, replacements = [], {}
        occupied = {t.text for t in tokens}
        for i, t in enumerate(tokens[:-3]):
            if t.text != '[' or not i or tokens[i - 1].text not in ('-', '<-'):
                continue
            if tokens[i + 1].text not in counts or tokens[i + 2].text != ':':
                continue
            name = tokens[i + 1].text
            uses = [k for k, x in enumerate(tokens) if x.kind in ('ident', 'quoted') and x.text == name]
            if any(k != i + 1 and not (k >= 2 and k + 1 < len(tokens) and tokens[k - 2].upper == 'COUNT'
                                      and tokens[k - 1].text == '(' and tokens[k + 1].text == ')') for k in uses):
                continue
            end = i + 3
            while end < len(tokens) and tokens[end].text != ']':
                end += 1
            if any(x.text == '*' for x in tokens[i:end]):
                continue
            node = end + 2
            if node + 1 >= len(tokens) or tokens[node].text != '(' or tokens[node + 1].text not in (':', ')'):
                continue
            name = 'cico_count_node'
            while name in occupied:
                name += '_'
            occupied.add(name)
            edits.append((node + 1, Tok('ident', name)))
            replacements[tokens[i + 1].text] = name
        for i, t in enumerate(tokens[:-3]):
            if t.upper == 'COUNT' and tokens[i + 1].text == '(' and tokens[i + 3].text == ')':
                token = tokens[i + 2]
                if token.text in replacements:
                    token.text = replacements[token.text]
        for position, token in reversed(edits):
            tokens.insert(position, token)
        return tokens

    # ------------------------------------------------------- plan hints
    HINT_END = {'WHERE', 'WITH', 'RETURN', 'MATCH', 'OPTIONAL', 'UNWIND', 'USING', 'ORDER', 'CALL', 'UNION',
                'CREATE', 'MERGE', 'SET', 'DELETE', 'DETACH', 'REMOVE', 'FOREACH', 'SKIP', 'LIMIT'}

    def _drop_hints(self, tokens):
        """`USING INDEX|SCAN|JOIN ...` only steers Neo4j's planner; dropping
        it keeps the meaning of the query."""
        out, i, depth = [], 0, 0
        while i < len(tokens):
            t = tokens[i]
            if depth == 0 and t.kind == 'ident' and t.upper == 'USING':
                i += 1
                inner = 0
                while i < len(tokens):
                    u = tokens[i]
                    if u.text in ('(', '[', '{'):
                        inner += 1
                    elif u.text in (')', ']', '}'):
                        inner -= 1
                    elif inner == 0 and u.kind == 'ident' and u.upper in self.HINT_END:
                        break
                    i += 1
                continue
            depth += t.text in ('(', '[', '{')
            depth -= t.text in (')', ']', '}')
            out.append(t)
            i += 1
        return out

    # ---------------------------------------------------- shortest paths
    def _shortest_paths(self, tokens):
        """`p = shortestPath((a)-[r:T*l..u]-(b))` -> `p = (a)-[r:T* SHORTEST l..u]-(b)`
        (`allShortestPaths` -> `ALL SHORTEST`).  Both return one / all
        shortest paths per endpoint pair.  Neo4j evaluates WHERE predicates
        on the path or its relationships during the search (the shortest path
        that satisfies them); the internal form filters afterwards, so such
        predicates are rejected."""
        out, i = [], 0
        while i < len(tokens):
            t = tokens[i]
            name = t.text.lower() if t.kind == 'ident' else None
            if name in ('shortestpath', 'allshortestpaths') and i + 1 < len(tokens) and tokens[i + 1].text == '(':
                end, inner = self._call_args(tokens, i + 1)
                stars = [k for k, x in enumerate(inner) if x.text == '*']
                opens = [k for k, x in enumerate(inner) if x.text == '[']
                if len(stars) != 1 or len(opens) != 1:
                    raise Unsupported(f'{t.text}() over a pattern with {len(opens)} relationships')
                rel_var = inner[opens[0] + 1].text if inner[opens[0] + 1].kind in ('ident', 'quoted') else None
                path_var = tokens[i - 2].text if i >= 2 and tokens[i - 1].text == '=' else None
                self._no_path_predicate(tokens, end, {v for v in (rel_var, path_var) if v}, t.text)
                mode = ['SHORTEST'] if name == 'shortestpath' else ['ALL', 'SHORTEST']
                k = stars[0] + 1
                inner = inner[:k] + [Tok('ident', w, ' ') for w in mode] + inner[k:]
                if k + len(mode) < len(inner) and inner[k + len(mode)].text not in (']',):
                    inner[k + len(mode)].ws = ' '
                inner[0].ws = t.ws
                out.extend(inner)
                i = end
                continue
            out.append(t)
            i += 1
        return out

    def _no_path_predicate(self, tokens, start, names, function):
        depth, i, in_where = 0, start, False
        while i < len(tokens):
            t = tokens[i]
            if t.text in ('(', '[', '{'):
                depth += 1
            elif t.text in (')', ']', '}'):
                depth -= 1
            elif depth == 0 and t.kind == 'ident' and t.upper in self.CLAUSE_STARTS:
                if t.upper != 'WHERE':
                    return
                in_where = True
            if in_where and t.kind in ('ident', 'quoted') and t.text in names:
                raise Unsupported(f'WHERE on a {function}() path or relationship: Neo4j applies it during the search')
            i += 1

    # ---------------------------------------------- relationship uniqueness
    CLAUSE_STARTS = {'MATCH', 'OPTIONAL', 'WITH', 'RETURN', 'UNWIND', 'WHERE', 'ORDER', 'SKIP', 'LIMIT',
                     'CALL', 'UNION', 'CREATE', 'MERGE', 'SET', 'DELETE', 'DETACH', 'REMOVE', 'FOREACH'}

    def _with_uniqueness(self, tokens):
        """Neo4j binds the relationships of one MATCH clause to distinct
        edges (relationship isomorphism); the internal semantics is a
        homomorphism.  Make the Neo4j rule explicit: `r1 <> r2` for every pair
        of fixed-length relationships of a clause that could bind the same
        edge.  (Variable-length relationships are trails in Neo4j and walks
        internally; they agree on acyclic relationship types such as REPLY_OF.)"""
        n, depth, bounds = len(tokens), 0, []
        for i, t in enumerate(tokens):
            if t.text in ('(', '[', '{'):
                depth += 1
            elif t.text in (')', ']', '}'):
                depth -= 1
            elif depth == 0 and t.kind == 'ident' and t.upper in self.CLAUSE_STARTS:
                if t.upper == 'MATCH' and bounds and bounds[-1][1] == 'OPTIONAL' and bounds[-1][0] == i - 1:
                    continue
                bounds.append((i, t.upper))
        bounds.append((n, 'END'))
        edits = []  # (position, tokens to insert)
        counter = [0]
        clauses, part = [], 0
        for k, (start, word) in enumerate(bounds[:-1]):
            if word in ('WITH', 'RETURN', 'UNION'):
                part += 1
            if word not in ('MATCH', 'OPTIONAL'):
                continue
            pattern_end = bounds[k + 1][0]
            where = bounds[k + 1][1] == 'WHERE'
            where_end = bounds[k + 2][0] if where else None
            rels = self._clause_rels(tokens, start + (2 if word == 'OPTIONAL' else 1), pattern_end)
            unequal = self._where_unequal(tokens, pattern_end + 1, where_end) if where else set()
            clauses.append((start, word, pattern_end, where, where_end, rels, unequal, part))
        facts = self.part_facts(clauses)
        for start, word, pattern_end, where, where_end, rels, unequal, part in clauses:
            pairs = []
            for i, a in enumerate(rels):
                for b in rels[i + 1:]:
                    if (a['varlen'] or b['varlen']) and (a['tables'] is None or b['tables'] is None
                                                         or a['tables'] & b['tables']):
                        # Neo4j also keeps a variable-length relationship's edges
                        # distinct from the clause's other relationships; that
                        # needs a list predicate the internal dialect lacks.
                        # Unless the part's own filters already remove every
                        # row in which they could share an edge (root paths).
                        if word == 'MATCH' and self._uniqueness_implied(a, b, *facts.get(part, ([], set()))):
                            continue
                        raise Unsupported('relationship uniqueness involving a variable-length relationship')
                    if self._may_share(a, b, unequal):
                        pairs.append((a, b))
            if not pairs:
                continue
            for rel in {id(r): r for pair in pairs for r in pair}.values():
                if rel['var'] is None:
                    counter[0] += 1
                    rel['var'] = f'cico_u{counter[0]}'
                    edits.append((rel['open'] + 1, [Tok('ident', rel['var'])]))
            # Compare internal ids: the binder rejects `r1 <> r2` between
            # relationships over different table sets (NeuG types.cpp assert).
            # Written as `r1 <> r2`; normalize.entity_id_comparisons turns it
            # into `id(r1) <> id(r2)` unless both declare the same tables (NeuG
            # asserts on mixed table sets, and in some multi-pattern MATCHes
            # fails on id() comparisons over equal table sets).
            text = ' AND '.join(f"{_q(a['var'])} <> {_q(b['var'])}" for a, b in pairs)
            if where:
                edits.append((pattern_end + 1, [Tok('punct', '(', ' ')]))
                edits.append((where_end, tokenize(f') AND {text}')))
            else:
                edits.append((pattern_end, tokenize(f' WHERE {text}')))
        if not edits:
            return tokens
        out = list(tokens)
        for position, inserted in sorted(edits, key=lambda e: e[0], reverse=True):
            out[position:position] = inserted
        return out

    def _clause_rels(self, tokens, start, end):
        rels = []
        for i in range(start, end):
            if tokens[i].text != '[' or i == 0 or tokens[i - 1].text not in ('-', '<-'):
                continue
            depth, j = 0, i
            while j < end:
                depth += tokens[j].text == '['
                depth -= tokens[j].text == ']'
                if depth == 0:
                    break
                j += 1
            inner = tokens[i + 1:j]
            var, k = None, 0
            if inner and inner[0].kind in ('ident', 'quoted') and (len(inner) == 1 or inner[1].text in (':', '*', '{')):
                var, k = _ident(inner[0]), 1
            types = []
            if k < len(inner) and inner[k].text == ':':
                k += 1
                while k < len(inner) and inner[k].kind in ('ident', 'quoted'):
                    types.append(_ident(inner[k]))
                    k += 1
                    if k < len(inner) and inner[k].text == '|':
                        k += 1
                        if k < len(inner) and inner[k].text == ':':
                            k += 1
                        continue
                    break
            varlen = k < len(inner) and inner[k].text == '*'
            left = tokens[i - 1].text
            right = tokens[j + 1].text if j + 1 < len(tokens) else ''
            prev_open = None
            if i >= 2 and tokens[i - 2].text == ')':
                d, k = 0, i - 2
                while k >= 0:
                    d += tokens[k].text == ')'
                    d -= tokens[k].text == '('
                    if d == 0:
                        break
                    k -= 1
                prev_open = k
            prev_var = self._peek_node(tokens, prev_open)[0] if prev_open is not None else None
            next_var = self._peek_node(tokens, j + 2)[0] if j + 2 < len(tokens) and tokens[j + 2].text == '(' else None
            before = self._endpoint_tables(tokens, prev_open) if prev_open is not None else None
            after = self._endpoint_tables(tokens, j + 2)
            tables = None
            if types:
                tables = list(dict.fromkeys(t for name in types for t in self.rels.get(name, [name])))
                if self.rel_pairs:
                    tables = [t for t in tables if t.upper() in self.rel_pairs] or tables
                if not varlen:
                    tables = self._prune(tables, left, right, before, after)
                tables = {t.upper() for t in tables}
            if left == '<-' and right != '->':
                src, dst, src_t, dst_t, directed = next_var, prev_var, after, before, True
            elif right == '->' and left != '<-':
                src, dst, src_t, dst_t, directed = prev_var, next_var, before, after, True
            else:
                src, dst, src_t, dst_t, directed = prev_var, next_var, before, after, False
            rels.append(dict(open=i, var=var, tables=tables, varlen=varlen, src=src, dst=dst,
                             src_t=src_t, dst_t=dst_t, directed=directed, types=types))
        return rels

    @staticmethod
    def part_facts(clauses):
        """Facts holding on every row of a QueryPart: mandatory MATCH patterns
        and their WHERE conjuncts (an OPTIONAL clause's WHERE filters nothing)."""
        facts = {}
        for _, word, _, _, _, rels, unequal, part in clauses:
            if word == 'MATCH':
                fact_rels, fact_unequal = facts.setdefault(part, ([], set()))
                fact_rels.extend(rels)
                fact_unequal.update(unequal)
        return facts

    def clause_table(self, tokens):
        """[(start, word, pattern_end, where, where_end, rels, unequal, part)] of a statement."""
        n, depth, bounds = len(tokens), 0, []
        for i, t in enumerate(tokens):
            if t.text in ('(', '[', '{'):
                depth += 1
            elif t.text in (')', ']', '}'):
                depth -= 1
            elif depth == 0 and t.kind == 'ident' and t.upper in self.CLAUSE_STARTS:
                if t.upper == 'MATCH' and bounds and bounds[-1][1] == 'OPTIONAL' and bounds[-1][0] == i - 1:
                    continue
                bounds.append((i, t.upper))
        bounds.append((n, 'END'))
        clauses, part = [], 0
        for k, (start, word) in enumerate(bounds[:-1]):
            if word in ('WITH', 'RETURN', 'UNION'):
                part += 1
            if word not in ('MATCH', 'OPTIONAL'):
                continue
            pattern_end = bounds[k + 1][0]
            where = bounds[k + 1][1] == 'WHERE'
            where_end = bounds[k + 2][0] if where else None
            rels = self._clause_rels(tokens, start + (2 if word == 'OPTIONAL' else 1), pattern_end)
            unequal = self._where_unequal(tokens, pattern_end + 1, where_end) if where else set()
            clauses.append((start, word, pattern_end, where, where_end, rels, unequal, part))
        return clauses

    # ------------------------------------- implied typed labels (relax stage)
    def _implied_typed_labels(self, tokens):
        """Replace a typed label (City) by its base label (Place) where
        declared endpoint_labels constraints imply it for every match, e.g.
        (p:Person)-[:IS_LOCATED_IN]->(:City) under (:Person)-[:IS_LOCATED_IN]->(:City).

        Types propagate along fixed-length, single-type, directed
        relationships from labels written in the statement (a variable's
        labels from all of its occurrences); the dropped label is then a
        predicate true on every match, so the pattern matches the same rows
        on every database satisfying the constraints.  Used only by the
        relax stage, after the ordinary attempt left the statement unchanged."""
        if not self.constraints.items:
            return tokens
        def node_start(close):
            depth, k = 0, close
            while k >= 0:
                depth += tokens[k].text == ')'
                depth -= tokens[k].text == '('
                if depth == 0:
                    return k
                k -= 1
            return None
        declared = {}
        for i, t in enumerate(tokens):
            if t.text == '(':
                var, labels = self._peek_node(tokens, i)
                if var and labels:
                    declared.setdefault(var, set()).update(labels)
        # Labels flow through `collect(x) AS L` (elements are x's values) and
        # `UNWIND L AS v` (v ranges over L's elements); only for names bound
        # once in the statement, so no other scope can reuse them.
        bound = {}
        for k in range(1, len(tokens)):
            if tokens[k - 1].upper == 'AS' and tokens[k].kind in ('ident', 'quoted'):
                bound[_ident(tokens[k])] = bound.get(_ident(tokens[k]), 0) + 1
        elements = {}
        for k in range(len(tokens) - 5):
            w = [t.text for t in tokens[k:k + 6]]
            if tokens[k].kind == 'ident' and tokens[k].upper == 'COLLECT' and w[1] == '(' and w[3] == ')' \
                    and tokens[k + 4].upper == 'AS' and tokens[k + 2].kind in ('ident', 'quoted'):
                x, target = _ident(tokens[k + 2]), _ident(tokens[k + 5])
                if bound.get(target) == 1 and declared.get(x):
                    elements[target] = set(declared[x])
        for k in range(len(tokens) - 3):
            if tokens[k].kind == 'ident' and tokens[k].upper == 'UNWIND' and tokens[k + 2].upper == 'AS' \
                    and tokens[k + 1].kind in ('ident', 'quoted') and tokens[k + 3].kind in ('ident', 'quoted'):
                source, var = _ident(tokens[k + 1]), _ident(tokens[k + 3])
                if bound.get(var) == 1 and source in elements:
                    declared.setdefault(var, set()).update(elements[source])
        occurrences, edges = {}, []
        for i in range(1, len(tokens)):
            if tokens[i].text != '[' or tokens[i - 1].text not in ('-', '<-'):
                continue
            try:
                close = closing_index(tokens, i)
            except Unsupported:
                continue
            inner = tokens[i + 1:close]
            if any(t.text == '*' for t in inner):
                continue
            types = [_ident(t) for k, t in enumerate(inner) if k and inner[k - 1].text in (':', '|')
                     and t.kind in ('ident', 'quoted')]
            left = node_start(i - 2) if i >= 2 and tokens[i - 2].text == ')' else None
            right = close + 2 if close + 2 < len(tokens) and tokens[close + 2].text == '(' else None
            if left is None or right is None or len(types) != 1:
                continue
            keys = []
            for at in (left, right):
                var, labels = self._peek_node(tokens, at)
                key = var or ('@', at)
                occurrences[at] = (key, labels)
                keys.append(key)
            into_right = tokens[close + 1].text == '->' and tokens[i - 1].text == '-'
            into_left = tokens[i - 1].text == '<-' and tokens[close + 1].text == '-'
            if into_right:
                edges.append((keys[0], types[0], keys[1]))
            elif into_left:
                edges.append((keys[1], types[0], keys[0]))
        known = {}
        for at, (key, labels) in occurrences.items():
            known.setdefault(key, set()).update(labels)
            if isinstance(key, str):
                known[key].update(declared.get(key, ()))
        implied, used = {}, {}
        changed = True
        while changed:
            changed = False
            for src, rel, dst in edges:
                for c in self.constraints.implied_endpoint(rel, known.get(src, set()) | implied.get(src, set())):
                    if c.dst not in implied.setdefault(dst, set()):
                        implied[dst].add(c.dst)
                        used[(dst, c.dst)] = c
                        changed = True
        out = list(tokens)
        for at, (key, labels) in sorted(occurrences.items(), reverse=True):
            for label in labels:
                if label not in self.typed or label not in implied.get(key, ()):
                    continue
                table = self.typed[label]['table'].upper()
                base = next((name for name, tables in self.labels.items()
                             if [t.upper() for t in tables] == [table]), None)
                if base is None:
                    continue
                for k in range(at + 1, len(out)):
                    if out[k].text == ')':
                        break
                    if out[k].kind in ('ident', 'quoted') and _ident(out[k]) == label and out[k - 1].text == ':':
                        out[k] = Tok(out[k].kind, _q(base), out[k].ws)
                        break
                self.premises.update(c.describe() for c in used.values())
        return out

    # ------------------------------------------- root paths (declared constraints)
    def _root_path(self, rel):
        """The constraint making a variable-length relationship the UNIQUE path
        from its start to a root, or None.

        Requires: one acyclic type T, written start -[:T*]-> end; every
        possible start node has at most one outgoing T (declared at_most_one);
        no end table has an outgoing T in the schema (the end is a root).
        Then the path is the start's root path R(start), ending at root(start),
        whatever its length bounds."""
        types = rel.get('types') or []
        if not rel['varlen'] or not rel['directed'] or len(types) != 1 or types[0] not in self.acyclic:
            return None
        if rel['src'] is None or rel['dst'] is None or not rel['src_t'] or not rel['dst_t']:
            return None
        functional = self.constraints.at_most_one_out(types[0], rel['src_t'])
        if functional is None:
            return None
        internal = {t.upper() for t in self.rels.get(types[0], [types[0]])}
        ends = {t.upper() for t in rel['dst_t']}
        if any(src in ends for group in internal for src, _ in self.rel_pairs.get(group, ())):
            return None
        return functional

    def _uniqueness_implied(self, a, b, rels, unequal):
        """True when no row surviving the part's filters can bind relationships
        a and b (one variable-length) to a common edge, so Neo4j's uniqueness
        between them removes nothing the filters keep.

        In a forest (acyclic T, at most one outgoing T per node) two root
        paths sharing an edge share their root.  A fixed edge x -[:T]-> y on
        R(s) gives root(s) = root(y); y = s would close a cycle.  The forced
        equality of roots is propagated through fixed relationships declared
        at_most_one (equal targets of an at-most-one-incoming relationship
        have equal sources, and vice versa); reaching a pair the part's
        mandatory filters keep unequal is a contradiction."""
        ra, rb = self._root_path(a), self._root_path(b)
        used = set()
        if ra and rb:
            if a['types'] != b['types']:
                return False
            equal, used = (a['dst'], b['dst']), {ra, rb}
        else:
            path, fixed, premise = (a, b, ra) if a['varlen'] else (b, a, rb)
            if premise is None or fixed['varlen'] or not fixed['directed'] or fixed.get('types') != path['types'] \
                    or fixed['dst'] is None:
                return False
            if fixed['dst'] == path['src']:
                self.premises.add(premise.describe())
                return True             # the fixed edge on R(start) would close a cycle
            other = next((r for r in rels if r is not path and r['varlen'] and r['src'] == fixed['dst']
                          and self._root_path(r)), None)
            if other is None:
                return False
            equal, used = (path['dst'], other['dst']), {premise, self._root_path(other)}
        found = self.contradiction([equal], rels, unequal)
        if found is None:
            return False
        self.premises.update(c.describe() for c in used | found)
        return True

    def contradiction(self, equalities, rels, unequal):
        """Constraints proving that the node equalities cannot hold on a row
        the part keeps, or None.  Equalities propagate through fixed directed
        relationships declared at_most_one (equal targets of an
        at-most-one-incoming relationship have equal sources, and vice
        versa) until a pair the part's mandatory filters keep unequal meets."""
        parent, used = {}, set()

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x

        def union(x, y):
            fx, fy = find(x), find(y)
            if fx != fy:
                parent[fx] = fy
                return True
            return False

        for x, y in equalities:
            if x is None or y is None:
                return None
            union(x, y)
        fixed = [r for r in rels if not r['varlen'] and r['directed'] and len(r.get('types') or []) == 1
                 and r['src'] is not None and r['dst'] is not None]
        changed = True
        while changed:
            changed = False
            for i, x in enumerate(fixed):
                for y in fixed[i + 1:]:
                    if x['types'] != y['types']:
                        continue
                    if find(x['dst']) == find(y['dst']) and find(x['src']) != find(y['src']):
                        c = self.constraints.at_most_one_in(x['types'][0], x['dst_t'] or ())
                        if c and self.constraints.at_most_one_in(x['types'][0], y['dst_t'] or ()):
                            changed |= union(x['src'], y['src'])
                            used.add(c)
                    if find(x['src']) == find(y['src']) and find(x['dst']) != find(y['dst']):
                        c = self.constraints.at_most_one_out(x['types'][0], x['src_t'] or ())
                        if c and self.constraints.at_most_one_out(x['types'][0], y['src_t'] or ()):
                            changed |= union(x['dst'], y['dst'])
                            used.add(c)
        if any(len(pair) == 2 and find(min(pair)) == find(max(pair)) for pair in unequal):
            return used
        return None

    @staticmethod
    def _where_unequal(tokens, start, end):
        """Top-level `x <> y` / `NOT x = y` conjuncts between variables."""
        pairs, depth, part = set(), 0, []
        def flush(part):
            texts = [t.text for t in part]
            if len(part) == 3 and part[0].kind == 'ident' and part[2].kind == 'ident' and texts[1] == '<>':
                pairs.add(frozenset((part[0].text, part[2].text)))
            if len(part) == 4 and part[0].upper == 'NOT' and part[1].kind == 'ident' and texts[2] == '=' \
                    and part[3].kind == 'ident':
                pairs.add(frozenset((part[1].text, part[3].text)))
        for t in tokens[start:end]:
            if t.text in ('(', '[', '{'):
                depth += 1
            elif t.text in (')', ']', '}'):
                depth -= 1
            if depth == 0 and t.kind == 'ident' and t.upper == 'AND':
                flush(part)
                part = []
            else:
                part.append(t)
        flush(part)
        return pairs

    @staticmethod
    def _may_share(a, b, unequal):
        if a['varlen'] or b['varlen']:
            return False  # see _with_uniqueness: trails vs walks, not expressible
        if a['tables'] is not None and b['tables'] is not None and not a['tables'] & b['tables']:
            return False
        if a['var'] and b['var'] and frozenset((a['var'], b['var'])) in unequal:
            return False
        def impossible(x, y, tx, ty):
            if x is not None and x == y:
                return False
            if x and y and frozenset((x, y)) in unequal:
                return True
            return bool(tx and ty and not {t.upper() for t in tx} & {t.upper() for t in ty})
        options = [((a['src'], b['src'], a['src_t'], b['src_t']), (a['dst'], b['dst'], a['dst_t'], b['dst_t']))]
        if not (a['directed'] and b['directed']):
            options.append(((a['src'], b['dst'], a['src_t'], b['dst_t']), (a['dst'], b['src'], a['dst_t'], b['src_t'])))
        return not all(impossible(*p) or impossible(*q) for p, q in options)

    def _collect_var_tables(self, tokens):
        found = {}
        for i, t in enumerate(tokens):
            if t.text != '(':
                continue
            var, labels = self._peek_node(tokens, i)
            if var and labels:
                try:
                    tables = {x.upper() for x in self._tables_for(labels)[0]}
                except Unsupported:
                    continue
                found[var] = found.get(var, tables) & tables
        return found

    @staticmethod
    def _peek_node(tokens, i):
        """(variable, labels) of a node pattern starting at tokens[i] == '('."""
        n, j = len(tokens), i + 1
        var = None
        if j < n and tokens[j].kind in ('ident', 'quoted') and not (j + 1 < n and tokens[j + 1].text == '('):
            var = _ident(tokens[j])
            j += 1
        labels = []
        while j + 1 < n and tokens[j].text == ':' and tokens[j + 1].kind in ('ident', 'quoted'):
            labels.append(_ident(tokens[j + 1]))
            j += 2
        if j < n and tokens[j].text not in (')', '{'):
            return None, []
        return var, labels

    def _endpoint_tables(self, tokens, i):
        """Node tables of the node pattern at tokens[i] (None = unconstrained)."""
        if i < 0 or i >= len(tokens) or tokens[i].text != '(':
            return None
        var, labels = self._peek_node(tokens, i)
        if labels:
            try:
                return {x.upper() for x in self._tables_for(labels)[0]}
            except Unsupported:
                return None
        return self.var_tables.get(var) if var else None

    def _prune(self, tables, left_arrow, right_arrow, before, after):
        """Keep relationship tables whose endpoint pairs fit the adjacent
        node tables.  A Neo4j type may map to several tables (HAS_TAG ->
        Comment_hasTag_Tag | Post_hasTag_Tag | ...); keeping incompatible ones
        changes nothing semantically but hides the pattern from FILL matching."""
        if not self.rel_pairs or (before is None and after is None):
            return tables
        def fits(a, b, pair):
            return (a is None or pair[0] in a) and (b is None or pair[1] in b)
        keep = []
        for table in tables:
            pairs = self.rel_pairs.get(table.upper())
            if pairs is None:
                keep.append(table)
                continue
            ok = False
            for pair in pairs:
                if right_arrow == '->' and left_arrow != '<-':
                    ok |= fits(before, after, pair)
                elif left_arrow == '<-' and right_arrow != '->':
                    ok |= fits(after, before, pair)
                else:
                    ok |= fits(before, after, pair) or fits(after, before, pair)
            if ok:
                keep.append(table)
        return keep or tables

    def parameter_types(self, types):
        return {k: ('TIMESTAMP' if v in ('DATETIME', 'ZONED DATETIME', 'LOCAL DATETIME') else v)
                for k, v in (types or {}).items()}

    def table_to_internal(self, name):
        if name in self.typed:
            raise Unsupported(f'typed label {name} cannot be a FILL target')
        tables = self.labels.get(name)
        if tables is None:
            return name
        if len(tables) != 1:
            raise Unsupported(f'label {name} maps to several tables')
        return tables[0]

    def ddl_to_internal(self, statement):
        def table(m):
            return m[1] + _q(self.table_to_internal(m[2].strip('`')))
        return re.sub(r'(\b(?:TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?|FROM|TO)\s+)(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)',
                      table, statement, flags=re.I)

    def _internal_vocabulary(self, text):
        """True when every label / relationship type in `text` is an internal
        table name: such a FILL (e.g. the NeuG index scripts) is already in
        the internal dialect and keeps its homomorphic matching semantics."""
        if not self.rel_pairs:
            return False
        tables = self.node_names | set(self.rel_pairs)
        text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", strip_comments(text))
        while re.search(r'\{[^{}]*\}', text):  # drop maps / subquery bodies' braces content
            text = re.sub(r'\{([^{}]*)\}', lambda m: '' if re.search(r'\w\s*:', m[1]) and 'MATCH' not in m[1].upper()
                          else m[1], text)
        names = re.findall(r'[:|]\s*`?([A-Za-z_][A-Za-z0-9_]*)', text)
        return bool(names) and all(n.upper() in tables for n in names)

    def fill_to_internal(self, fill_text):
        if self._internal_vocabulary(fill_text):
            return fill_text
        m = re.match(r'\s*FILL\s+(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)(\s*\([^)]*\)\s*FROM\s*\()(.*)\)\s*;?\s*$',
                     strip_comments(fill_text), re.I | re.S)
        if not m:
            return fill_text
        table = m[1].strip('`')
        target = self.rels.get(table, [table])[0] if table in self.rels else self.table_to_internal(table)
        return f'FILL {_q(target)}{m[2]}{self.to_internal(m[3])})'

    # -------------------------------------------------------- translation
    def _translate(self, tokens, map_body=False):
        out, i, n = [], 0, len(tokens)
        pattern_mode = False
        brace_stack = []  # 'map' | 'block'
        while i < n:
            t = tokens[i]
            up = t.upper if t.kind == 'ident' else None
            if up == 'CALL':
                raise Unsupported('CALL subquery' if i + 1 < n and tokens[i + 1].text in ('{', '(')
                                  else 'CALL procedure: no internal equivalent')
            if not pattern_mode and t.kind in ('ident', 'quoted') and i + 1 < n and tokens[i + 1].text == '{' \
                    and up not in ('EXISTS', 'COUNT', 'COLLECT') and not (out and out[-1].text == '.'):
                raise Unsupported('map projection: no internal map/struct literal')
            if up in PATTERN_KEYWORDS:
                pattern_mode = True
            elif up in CLAUSE_END:
                pattern_mode = False
            if t.text == '{':
                prev = tokens[i - 1] if i else None
                block = prev is not None and prev.kind == 'ident' and prev.upper in ('EXISTS', 'COUNT', 'COLLECT', 'CALL')
                brace_stack.append('block' if block else 'map')
                out.append(t)
                i += 1
                continue
            if t.text == '}':
                if brace_stack and brace_stack.pop() == 'block':
                    pattern_mode = False
                out.append(t)
                i += 1
                continue
            in_map = (bool(brace_stack) and brace_stack[-1] == 'map') or (map_body and not brace_stack)
            if t.text == '(' and not in_map:
                node = self._node(tokens, i)
                if node is not None and (pattern_mode or self._starts_pattern(tokens, node[0])):
                    if not pattern_mode:
                        # Pattern predicate in an expression: (a)-[:R]->(b)
                        end, text = self._pattern_predicate(tokens, i)
                        out.extend(tokenize(text))
                        out[-len(tokenize(text))].ws = t.ws
                        i = end
                        continue
                    end, replacement = node
                    replacement[0].ws = t.ws
                    out.extend(replacement)
                    i = end
                    continue
            if t.text == '[' and pattern_mode and out and out[-1].text in ('-', '<-'):
                end, replacement, both = self._rel(tokens, i)
                if both:  # <-[..]-> : undirected
                    out[-1].text = '-'
                replacement[0].ws = t.ws
                out.extend(replacement)
                i = end
                if both:
                    tokens[i].text = '-'
                continue
            if not pattern_mode and not in_map and t.kind in ('ident', 'quoted') and i + 2 < n \
                    and tokens[i + 1].text == ':' and tokens[i + 2].kind in ('ident', 'quoted') \
                    and not (out and out[-1].text in ('.', '{', ',')):
                # label predicate x:Label (outside patterns and maps)
                text = self._label_predicate(_ident(t), _ident(tokens[i + 2]))
                out.extend(tokenize(text))
                out[-len(tokenize(text))].ws = t.ws
                i += 3
                continue
            if t.kind == 'ident' and i + 1 < n and tokens[i + 1].text == '(':
                func = t.text.lower()
                if func in ('tofloat', 'tointeger', 'tostring'):
                    end, inner = self._call_args(tokens, i + 1)
                    target = {'tofloat': 'DOUBLE', 'tointeger': 'INT64', 'tostring': 'STRING'}[func]
                    body = render(self._translate(inner))
                    out.extend(tokenize(f'CAST({body} AS {target})'))
                    out[-len(tokenize(f'CAST({body} AS {target})'))].ws = t.ws
                    i = end
                    continue
                if func in ('datetime', 'localdatetime'):
                    end, inner = self._call_args(tokens, i + 1)
                    if inner and inner[0].text == '{':
                        raise Unsupported(f'{t.text}({{...}}) from a map: no internal equivalent')
                    if len(inner) == 1 and inner[0].kind == 'string':
                        # literal: timestamp('...') folds to a TIMESTAMP literal
                        out.append(Tok('ident', 'timestamp', t.ws))
                        i += 1
                        continue
                    # value conversion: the same CAST the binder inserts for an
                    # implicitly typed operand, so printed casts rebind equal
                    text = f'CAST({render(self._translate(inner))} AS TIMESTAMP)'
                    out.extend(tokenize(text))
                    out[-len(tokenize(text))].ws = t.ws
                    i = end
                    continue
                if func == 'round':
                    from .normalize import call_args
                    end, args = call_args(tokens, i + 1)
                    if len(args) == 1:
                        # Neo4j round(x) == round(x, 0) (checked on Neo4j 5.26 for
                        # halves, negatives, large values, null); NeuG has only
                        # round(x, n).
                        text = f'round({render(self._translate(args[0]))}, 0)'
                        out.extend(tokenize(text))
                        out[-len(tokenize(text))].ws = t.ws
                        i = end
                        continue
                if func == 'duration':
                    end, inner = self._call_args(tokens, i + 1)
                    text = self._duration(inner)
                    out.extend(tokenize(text))
                    out[-len(tokenize(text))].ws = t.ws
                    i = end
                    continue
            if t.kind in ('ident', 'quoted') and i + 2 < n and tokens[i + 1].text == '.' \
                    and tokens[i + 2].kind in ('ident', 'quoted') and not (out and out[-1].text == '.') \
                    and not self._temporal(tokens, i):
                renamed = self._property(_ident(t), _ident(tokens[i + 2]))
                if renamed is not None:
                    out.extend([t, tokens[i + 1], Tok(tokens[i + 2].kind, _q(renamed), tokens[i + 2].ws)])
                    i += 3
                    continue
            if (t.kind == 'param' or t.kind == 'ident') and self._temporal(tokens, i):
                end, text = self._temporal(tokens, i)
                out.extend(tokenize(text))
                out[-len(tokenize(text))].ws = t.ws
                i = end
                continue
            out.append(t)
            i += 1
        return out

    ISO_UNITS = {'Y': 'years', 'M': 'months', 'W': 'weeks', 'D': 'days'}
    ISO_TIME_UNITS = {'H': 'hours', 'M': 'minutes', 'S': 'seconds'}
    MAP_UNITS = {'years', 'months', 'weeks', 'days', 'hours', 'minutes', 'seconds', 'milliseconds'}

    def _duration(self, inner):
        """duration({hours: x}) / duration('PT4H') -> internal duration('4 hours')."""
        if len(inner) == 1 and inner[0].kind == 'string':
            iso = inner[0].text[1:-1]
            m = re.fullmatch(r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', iso)
            if not m or not any(m.groups()):
                raise Unsupported(f'Neo4j duration {iso!r}')
            units = ['years', 'months', 'weeks', 'days', 'hours', 'minutes', 'seconds']
            parts = [f'{v} {u}' for v, u in zip(m.groups(), units) if v]
            return "duration('" + ' '.join(parts) + "')"
        if inner and inner[0].text == '{' and inner[-1].text == '}':
            body = inner[1:-1]
            if len(body) >= 3 and body[0].kind == 'ident' and body[1].text == ':' \
                    and body[0].text in self.MAP_UNITS and not any(t.text == ',' for t in body):
                value = render(self._translate(body[2:])).strip()
                return f"duration(CAST({value} AS STRING) + ' {body[0].text}')"
        raise Unsupported('Neo4j duration() form')

    @staticmethod
    def _temporal(tokens, i):
        """$p.year / a.b.year -> date_part('year', ...)."""
        n = len(tokens)
        if i and tokens[i - 1].text == '.':
            return None
        j = i + 1
        base = [tokens[i]]
        if tokens[i].kind == 'ident' and j + 1 < n and tokens[j].text == '.' and tokens[j + 1].kind in ('ident', 'quoted'):
            base += [tokens[j], tokens[j + 1]]
            j += 2
        if j + 1 < n and tokens[j].text == '.' and tokens[j + 1].kind == 'ident' \
                and tokens[j + 1].text.lower() in TEMPORAL_FIELDS and (len(base) == 3 or tokens[i].kind == 'param'):
            return j + 2, f"date_part('{tokens[j + 1].text.lower()}', {''.join(b.text for b in base)})"
        return None

    @staticmethod
    def _call_args(tokens, open_index):
        depth, j = 0, open_index
        while j < len(tokens):
            if tokens[j].text in ('(', '[', '{'):
                depth += 1
            elif tokens[j].text in (')', ']', '}'):
                depth -= 1
                if depth == 0:
                    return j + 1, tokens[open_index + 1:j]
            j += 1
        raise Unsupported('unbalanced parentheses')

    # ------------------------------------------------------------- nodes
    def _tables_for(self, labels):
        tables, predicates = None, []
        for label in labels:
            if label in self.typed:
                spec = self.typed[label]
                current = {spec['table']}
                predicates.append((spec['property'], spec['value']))
            elif label in self.labels:
                current = set(self.labels[label])
            else:
                current = {label}
            tables = current if tables is None else {t for t in tables if t.upper() in {c.upper() for c in current}}
        if tables is not None and not tables:
            raise Unsupported(f'labels {labels} have no common table')
        return tables, predicates

    def _node(self, tokens, i):
        """Parse `(var? (:Label)* {map}?)` at tokens[i]; return (end, new tokens)."""
        n, j = len(tokens), i + 1
        var = None
        if j < n and tokens[j].kind in ('ident', 'quoted') and not (j + 1 < n and tokens[j + 1].text == '('):
            var = tokens[j]
            j += 1
        labels = []
        while j + 1 < n and tokens[j].text == ':' and tokens[j + 1].kind in ('ident', 'quoted'):
            labels.append(_ident(tokens[j + 1]))
            j += 2
        union = None
        if j + 1 < n and tokens[j].text == '|' and len(labels) == 1:
            union = [labels[0]]
            while j + 1 < n and tokens[j].text == '|' and tokens[j + 1].kind in ('ident', 'quoted'):
                union.append(_ident(tokens[j + 1]))
                j += 2
        if j < n and tokens[j].text in ('&', '|', '!', '%'):
            raise Unsupported('Neo4j label expression')
        map_tokens = None
        if j < n and tokens[j].text == '{':
            depth, k = 0, j
            while k < n:
                depth += tokens[k].text == '{'
                depth -= tokens[k].text == '}'
                if depth == 0:
                    break
                k += 1
            map_tokens = tokens[j + 1:k]
            j = k + 1
        if j >= n or tokens[j].text != ')':
            return None
        if var is None and not labels and map_tokens is None:
            # `()` anonymous node: only a pattern if followed by a relationship
            pass
        text = '(' + (var.text if var else '')
        if union:
            # (x:A|B): any of the labels -> union of their tables
            if any(label in self.typed for label in union):
                raise Unsupported('label disjunction with a typed label')
            order = [t for label in union for t in (self.labels.get(label) or [label])]
            text += ''.join(':' + _q(t) for t in dict.fromkeys(order))
            tables, predicates = set(order), []
        elif labels:
            tables, predicates = self._tables_for(labels)
            order = [t for label in labels for t in (self.labels.get(label) or [self.typed.get(label, {}).get('table', label)])]
            text += ''.join(':' + _q(t) for t in dict.fromkeys(o for o in order if o in tables))
        else:
            predicates = []
            fresh = var is None or not self._in_scope(tokens, i, var.text)
            inferred = self._adjacent_tables(tokens, i, j + 1) if fresh else None
            if inferred and inferred != self.node_names:
                # An unlabelled node next to a relationship can only be one of
                # the relationship's endpoint tables; naming them is implied by
                # the pattern and keeps NeuG from binding the node to every
                # table (which breaks e.g. `x IN list` casts in the binder).
                text += ''.join(':' + _q(self.node_case.get(t, t)) for t in sorted(inferred))
        if map_tokens is not None and labels:
            map_tokens = self._rename_keys(map_tokens, tables)
        entries = render(self._translate(map_tokens, map_body=True)) if map_tokens is not None else ''
        extra = ', '.join(f"{_q(p)}: '{v}'" for p, v in predicates)
        if entries or extra:
            text += ' {' + ', '.join(x for x in (entries, extra) if x) + '}'
        return j + 1, tokenize(text + ')')

    def _starts_pattern(self, tokens, end):
        return end < len(tokens) and tokens[end].text in ('-', '<-', '<')

    def _pattern_predicate(self, tokens, i):
        """Translate an expression-level pattern `(a)-[..]->(b)...` into EXISTS."""
        parts, j = [], i
        node = self._node(tokens, j)
        parts.append(render(node[1]))
        j = node[0]
        while j < len(tokens) and tokens[j].text in ('-', '<-'):
            left = tokens[j].text
            j += 1
            if j < len(tokens) and tokens[j].text == '[':
                end, rel, both = self._rel(tokens, j)
                j = end
                body = render(rel)
            else:
                body, both = '', False
            if j >= len(tokens) or tokens[j].text not in ('-', '->'):
                raise Unsupported('pattern predicate')
            right = tokens[j].text
            j += 1
            if both:
                left, right = '-', '-'
            node = self._node(tokens, j)
            if node is None:
                raise Unsupported('pattern predicate node')
            parts.append(f'{left}{body}{right}{render(node[1])}')
            j = node[0]
        return j, 'EXISTS { MATCH ' + ''.join(parts) + ' }'

    # -------------------------------------------------------------- rels
    def _rel(self, tokens, i):
        n, j = len(tokens), i + 1
        out = '['
        if j < n and tokens[j].kind in ('ident', 'quoted'):
            out += tokens[j].text
            j += 1
        head = len(out)  # types are inserted after the variable
        types = []
        if j < n and tokens[j].text == ':':
            j += 1
            while j < n and tokens[j].kind in ('ident', 'quoted'):
                types.append(_ident(tokens[j]))
                j += 1
                if j < n and tokens[j].text == '|':
                    j += 1
                    if j < n and tokens[j].text == ':':
                        j += 1
                    continue
                break
        mapped = list(dict.fromkeys(t for name in types for t in self.rels.get(name, [name])))
        if self.rel_pairs:  # a mapping may list tables this store does not have
            mapped = [t for t in mapped if t.upper() in self.rel_pairs] or mapped
        star = j
        if j < n and tokens[j].text == '*':
            j += 1
            mode = None
            if j < n and tokens[j].kind == 'ident' and tokens[j].upper in ('SHORTEST', 'ALL'):
                mode = 'ALL SHORTEST' if tokens[j].upper == 'ALL' else 'SHORTEST'
                j += 2 if mode == 'ALL SHORTEST' else 1
            elif not types or any(name not in self.acyclic for name in types):
                # Neo4j variable-length relationships are trails (no edge
                # repeated); internally they are walks.  The two agree only on
                # acyclic types (mapping: acyclic_rel_types), so others are
                # translated as trails.
                mode = 'TRAIL'
            lower = upper = None
            dots = False
            if j < n and tokens[j].kind == 'number':
                lower = int(tokens[j].text)
                j += 1
            if j < n and tokens[j].text == '..':
                dots = True
                j += 1
                if j < n and tokens[j].kind == 'number':
                    upper = int(tokens[j].text)
                    j += 1
            if not dots:
                lower, upper = (lower, lower) if lower is not None else (1, UNBOUNDED)
            lower = 1 if lower is None else lower
            if mode in ('SHORTEST', 'ALL SHORTEST') and lower != 1:
                raise Unsupported(f'shortest path with lower bound {lower}')
            out += '*' + (f' {mode} ' if mode else '') + f"{lower}..{UNBOUNDED if upper is None else upper}"
        if j < n and tokens[j].text == '{':
            end, inner = self._call_args(tokens, j)
            out += ' {' + render(self._translate(inner, map_body=True)) + '}'
            j = end
        if j >= n or tokens[j].text != ']':
            raise Unsupported('relationship pattern')
        both = tokens[i - 1].text == '<-' and j + 1 < n and tokens[j + 1].text == '->'
        if mapped and tokens[star].text != '*':
            # fixed-length only: intermediate hops of a path may use any table
            before = None
            if i >= 2 and tokens[i - 2].text == ')':
                depth, k = 0, i - 2
                while k >= 0:
                    depth += tokens[k].text == ')'
                    depth -= tokens[k].text == '('
                    if depth == 0:
                        break
                    k -= 1
                before = self._endpoint_tables(tokens, k)
            right = tokens[j + 1].text if j + 1 < n else ''
            after = self._endpoint_tables(tokens, j + 2)
            mapped = self._prune(mapped, tokens[i - 1].text, right, before, after)
        if mapped:
            out = out[:head] + ':' + '|'.join(_q(t) for t in mapped) + out[head:]
        return j + 1, tokenize(out + ']'), both

    @staticmethod
    def _in_scope(tokens, i, name):
        """Is `name` already bound at tokens[i]?  Bound = projected by the
        last top-level WITH before i, or mentioned after that projection
        (or anywhere before i when there is no WITH)."""
        depth, last_with = 0, None
        for k in range(i):
            t = tokens[k]
            depth += t.text in ('(', '[', '{')
            depth -= t.text in (')', ']', '}')
            if depth == 0 and t.kind == 'ident' and t.upper == 'WITH':
                last_with = k
        if last_with is None:
            return any(x.kind in ('ident', 'quoted') and x.text == name for x in tokens[:i])
        # projection items of that WITH
        k, depth, item, names = last_with + 1, 0, [], set()
        ends = {'WHERE', 'ORDER', 'SKIP', 'LIMIT', 'MATCH', 'OPTIONAL', 'UNWIND', 'WITH', 'RETURN', 'CALL'}
        while k < i:
            t = tokens[k]
            if depth == 0 and t.kind == 'ident' and t.upper in ends:
                break
            depth += t.text in ('(', '[', '{')
            depth -= t.text in (')', ']', '}')
            if depth == 0 and t.text == ',':
                names.add(item[-1].text if item else None)
                item = []
            elif not (depth == 0 and t.kind == 'ident' and t.upper == 'DISTINCT' and not item):
                item.append(t)
            k += 1
        if item:
            names.add(item[-1].text)  # `x` or `expr AS x`
        if name in names or (len(item) == 1 and item[0].text == '*'):
            return True
        return any(x.kind in ('ident', 'quoted') and x.text == name for x in tokens[k:i])

    def _adjacent_tables(self, tokens, start, end):
        """Node tables implied by the relationships next to the node pattern
        tokens[start:end] (None = unconstrained)."""
        n, found = len(tokens), []
        if end + 1 < n and tokens[end].text in ('-', '<-') and tokens[end + 1].text == '[':
            close, depth = end + 1, 0
            while close < n:
                depth += tokens[close].text == '['
                depth -= tokens[close].text == ']'
                if depth == 0:
                    break
                close += 1
            left, right = tokens[end].text, tokens[close + 1].text if close + 1 < n else ''
            role = 'dst' if left == '<-' and right != '->' else 'src' if right == '->' and left != '<-' else 'either'
            found.append(self._rel_endpoint(tokens[end + 2:close], role))
        if start >= 2 and tokens[start - 1].text in ('-', '->') and tokens[start - 2].text == ']':
            k, depth = start - 2, 0
            while k >= 0:
                depth += tokens[k].text == ']'
                depth -= tokens[k].text == '['
                if depth == 0:
                    break
                k -= 1
            left, right = tokens[k - 1].text if k else '', tokens[start - 1].text
            role = 'dst' if right == '->' and left != '<-' else 'src' if left == '<-' and right != '->' else 'either'
            found.append(self._rel_endpoint(tokens[k + 1:start - 2], role))
        sets = [f for f in found if f is not None]
        if not sets:
            return None
        out = set.intersection(*sets)
        return out or None

    def _rel_endpoint(self, inner, role):
        if not self.rel_pairs:
            return None
        k = 1 if inner and inner[0].kind in ('ident', 'quoted') else 0
        types = []
        if k < len(inner) and inner[k].text == ':':
            k += 1
            while k < len(inner) and inner[k].kind in ('ident', 'quoted'):
                types.append(_ident(inner[k]))
                k += 1
                if k < len(inner) and inner[k].text == '|':
                    k += 1
                    if k < len(inner) and inner[k].text == ':':
                        k += 1
                    continue
                break
        if not types:
            return None
        if k < len(inner) and inner[k].text == '*':
            rest = [t for t in inner[k + 1:] if t.kind != 'ident']
            if not rest or rest[0].kind != 'number' or int(rest[0].text) < 1:
                return None  # a zero-length path: the node may be the other end
        tables = [t for name in types for t in self.rels.get(name, [name]) if t.upper() in self.rel_pairs]
        if not tables:
            return None
        out = set()
        for table in tables:
            for src, dst in self.rel_pairs[table.upper()]:
                if role in ('src', 'either'):
                    out.add(src)
                if role in ('dst', 'either'):
                    out.add(dst)
        return out

    def _renames(self, tables, prop):
        names = {self.prop_names.get(t.upper(), {}).get(prop, prop) for t in tables}
        if len(names) > 1:
            raise Unsupported(f'property {prop} has different internal names on {sorted(tables)}')
        name = names.pop()
        return None if name == prop else name

    def _property(self, var, prop):
        tables = self.var_tables.get(var)
        if not tables or not self.prop_names:
            return None
        return self._renames(tables, prop)

    def _rename_keys(self, tokens, tables):
        if not self.prop_names:
            return tokens
        out, depth = [], 0
        for k, t in enumerate(tokens):
            depth += t.text in ('(', '[', '{')
            depth -= t.text in (')', ']', '}')
            if depth == 0 and t.kind in ('ident', 'quoted') and k + 1 < len(tokens) and tokens[k + 1].text == ':' \
                    and (k == 0 or tokens[k - 1].text == ','):
                renamed = self._renames(tables, _ident(t))
                if renamed is not None:
                    t = Tok(t.kind, _q(renamed), t.ws)
            out.append(t)
        return out

    def _label_predicate(self, var, label):
        if label in self.typed:
            spec = self.typed[label]
            return f"({_q(var)}.{_q(spec['property'])} = '{spec['value']}')"
        tables = self.labels.get(label, [label])
        if len(tables) == 1:
            return f"(label({_q(var)}) = '{tables[0]}')"
        return f"(label({_q(var)}) IN [{', '.join(repr(t) for t in tables)}])"


def _conjuncts(e):
    if e is None:
        return []
    if e.get('function') == 'AND' or e.get('k') == 'AND':
        return [c for child in e.get('children') or [] for c in _conjuncts(child)]
    return [e]


def _ref(e):
    """Entity uid referenced by `e` (entity itself or its internal id)."""
    if e is None:
        return None
    if e.get('entity'):
        return e['entity']
    if e.get('k') == 'PROPERTY' and e.get('property') == '_ID':
        return e.get('owner')
    return None


def _unequal(predicate):
    pairs = set()
    for c in _conjuncts(predicate):
        if c.get('function') == 'NOT_EQUALS' and len(c.get('children') or []) == 2:
            a, b = (_ref(x) for x in c['children'])
            if a and b:
                pairs.add(frozenset((a, b)))
    return pairs


def may_share_edge(a, b, predicate, entity_tables, rel_group):
    """Could relationships a and b (IR) bind the same edge?"""
    def tables(rel):
        return {rel_group.get(t, t).upper() for t in rel.get('tables') or []}
    if not tables(a) & tables(b):
        return False
    unequal = _unequal(predicate)
    if frozenset((a['uid'], b['uid'])) in unequal:
        return False
    if a.get('recursive') is None and b.get('recursive') is None:
        def nodes(u):
            return {t.upper() for t in entity_tables.get(u) or []}
        ends = []
        if a.get('direction') == 'SINGLE' and b.get('direction') == 'SINGLE':
            ends = [((a['src'], b['src']), (a['dst'], b['dst']))]
        else:  # undirected: either orientation
            ends = [((a['src'], b['src']), (a['dst'], b['dst'])), ((a['src'], b['dst']), (a['dst'], b['src']))]
        def impossible(pair):
            x, y = pair
            if x == y:
                return False
            if frozenset((x, y)) in unequal:
                return True
            nx, ny = nodes(x), nodes(y)
            return bool(nx and ny and not nx & ny)
        if all(impossible(p) or impossible(q) for p, q in ends):
            return False
    return True


class Neo4jDialect(Dialect):
    """Bound IR -> Neo4j Cypher with the profile's schema mapping."""
    name = 'neo4j'

    def __init__(self, mapping):
        super().__init__()
        self.m = mapping
        self.table_sets = {frozenset(t.upper() for t in tables): label for label, tables in mapping['labels'].items()}
        self.single = {}
        for label, tables in mapping['labels'].items():
            if len(tables) == 1:
                self.single.setdefault(tables[0].upper(), label)
        self.typed = {(spec['table'].upper(), spec['property'], spec['value']): label
                      for label, spec in mapping['typed_labels'].items()}
        self.rel_names = {}
        for name, tables in mapping['rel_types'].items():
            for table in tables:
                self.rel_names.setdefault(table.upper(), name)
        self.entity_tables = {}
        self.prop_back = {t.upper(): {internal: name for name, internal in m.items()}
                          for t, m in mapping['properties'].items()}

    def property(self, owner, prop, uid=None):
        if self.prop_back:
            tables = self.entity_tables.get(uid) or []
            names = {self.prop_back.get(t.upper(), {}).get(prop, prop) for t in tables} or {prop}
            if len(names) > 1:
                raise Unsupported(f'property {prop} has different Neo4j names on {sorted(tables)}')
            prop = names.pop()
        return super().property(owner, prop, uid)

    def node_pattern(self, variable, tables):
        key = frozenset(t.upper() for t in tables)
        if key in self.table_sets:
            labels = [self.table_sets[key]]
        else:
            labels = [self.single.get(t.upper(), t) for t in tables]
        if len(labels) == 1:
            return self.ident(variable) + ':' + self.ident(labels[0])
        return self.ident(variable) + ':' + '|'.join(self.ident(label) for label in labels)

    def rel_types(self, tables):
        groups = list(dict.fromkeys(self.rel_group.get(t, t) for t in tables))
        names = list(dict.fromkeys(self.rel_names.get(g.upper(), g) for g in groups))
        return (':' + '|'.join(self.ident(n) for n in names)) if names else ''

    # Neo4j is schemaless: carriers need no DDL.
    def add_column_ddl(self, table, column, type_text, default):
        return []

    def create_rel_table_ddl(self, item, schema):
        return []

    BATCH_ROWS = 10000
    PARTITION_ROWS = 1000
    create_on_label_union = True

    def fill_statement(self, body, tail, variables, eager=False, partition=None):
        # Large writes in bounded transactions (auto-commit session required).
        scope = ', '.join(self.ident(v) for v in variables)
        if eager and partition is not None:
            # New relationships on nodes the source traverses: drive the build
            # by a label scan of one key node and run the source per node, so
            # no outer read outlives a committed batch and memory stays bounded.
            var, tables = partition
            return (f'MATCH ({self.node_pattern(var, tables)}) CALL ({self.ident(var)}) {{ {body} {tail} }} '
                    f'IN TRANSACTIONS OF {self.PARTITION_ROWS} ROWS')
        if not eager:
            return f'{body} CALL ({scope}) {{ {tail} }} IN TRANSACTIONS OF {self.BATCH_ROWS} ROWS'
        # New relationships on nodes the source still traverses: materialize
        # the source rows first, otherwise the outer read becomes outdated.
        unpack = ', '.join(f'cico_row[{i}] AS {self.ident(v)}' for i, v in enumerate(variables))
        return (f'{body} WITH collect([{scope}]) AS cico_rows UNWIND cico_rows AS cico_row '
                f'CALL (cico_row) {{ WITH {unpack} {tail} }} IN TRANSACTIONS OF {self.BATCH_ROWS} ROWS')

    def var_length(self, lower, upper):
        return f'*{lower}..' if upper == UNBOUNDED else f'*{lower}..{upper}'

    def label_of(self, arg):
        raise Unsupported('label() has no exact Neo4j equivalent')

    # ---------------------------------------------- relationship uniqueness
    def match_text(self, keyword, parts, predicate):
        """Neo4j binds the relationships of one MATCH clause to pairwise
        distinct edges; the internal (Kuzu/NeuG) semantics does not.  A
        mandatory MATCH is therefore printed as one MATCH per relationship
        (no uniqueness across clauses).  An OPTIONAL MATCH cannot be split, so
        it is kept only when its relationships can never bind the same edge."""
        rels = [r for _, r in parts if r is not None]
        if len(rels) <= 1:
            return super().match_text(keyword, parts, predicate)
        if keyword == 'MATCH':
            return 'MATCH ' + ' MATCH '.join(text for text, _ in parts)
        for i, a in enumerate(rels):
            for b in rels[i + 1:]:
                if may_share_edge(a, b, predicate, self.entity_tables, self.rel_group):
                    raise Unsupported('OPTIONAL MATCH whose relationships Neo4j would force distinct')
        return super().match_text(keyword, parts, predicate)

    def special_expression(self, e, printer):
        """label(x) = 'TABLE'  ->  (x:Label)   (Neo4j label predicate)."""
        if e.get('function') != 'EQUALS' or len(e.get('children') or []) != 2:
            return None
        for label, literal in (e['children'], e['children'][::-1]):
            arg = (label.get('children') or [None])[0]
            if label.get('function') == 'LABEL' and arg and arg.get('property') == '_ID' \
                    and literal.get('k') == 'LITERAL' and (literal.get('value') or {}).get('type') == 'STRING':
                table = literal['value'].get('text')
                name = self.single.get(str(table).upper())
                if name is None and not any(str(table).upper() == t.upper()
                                            for tables in self.m['labels'].values() for t in tables):
                    name = table  # unmapped: the Neo4j label is the table name
                if name is None:
                    return None
                return f'({self.ident(printer.entity_name(arg["owner"]))}:{self.ident(name)})'
        return None

    def temporal_literal(self, type_name, text):
        if type_name == 'DATE':
            return f'date({self.string(text)})'
        if type_name.startswith('TIMESTAMP'):
            return f"datetime({self.string(text.replace(' ', 'T', 1))})"
        raise Unsupported(f'literal type {type_name} for Neo4j')

    def literal(self, v):
        if v.get('null') and v.get('type') not in ('ANY', 'NULL', ''):
            raise Unsupported('typed NULL for Neo4j')
        return super().literal(v)

    def cast(self, arg, type_name):
        function = {'DOUBLE': 'toFloat', 'FLOAT': 'toFloat', 'INT64': 'toInteger', 'INT32': 'toInteger',
                    'STRING': 'toString', 'TIMESTAMP': 'datetime', 'DATE': 'date'}.get(type_name)
        if function is None:
            raise Unsupported(f'CAST to {type_name} for Neo4j')
        return f'{function}({arg})'

    SPELLING = {'TIMESTAMP': 'datetime', 'DATE': 'date', 'LOWER': 'toLower', 'UPPER': 'toUpper',
                'TO_INTERVAL': None, 'INTERVAL': None, 'LABEL': None, 'LABELS': None}

    INTERVAL_UNITS = {'year': ('Y', False), 'month': ('M', False), 'week': ('W', False), 'day': ('D', False),
                      'hour': ('H', True), 'minute': ('M', True), 'second': ('S', True)}

    def _iso_duration(self, text):
        m = re.fullmatch(r"'((?:\s*\d+\s+[a-zA-Z]+)+)\s*'", text)
        if not m:
            return None
        date, time = '', ''
        for value, unit in re.findall(r'(\d+)\s+([a-zA-Z]+)', m[1]):
            spec = self.INTERVAL_UNITS.get(unit.lower().rstrip('s'))
            if spec is None:
                return None
            if spec[1]:
                time += value + spec[0]
            else:
                date += value + spec[0]
        return "duration('P" + date + ('T' + time if time else '') + "')"

    def call(self, name, args):
        if name == 'TO_INTERVAL' and len(args) == 1:
            iso = self._iso_duration(args[0])
            if iso is None:
                raise Unsupported('non-literal interval for Neo4j')
            return iso
        if name == 'DATE_PART' and len(args) == 2:
            unit = re.fullmatch(r"'([a-z]+)'", args[0])
            if not unit or unit[1] not in TEMPORAL_FIELDS:
                raise Unsupported('date_part unit')
            if not re.fullmatch(r'(\$?[A-Za-z_][A-Za-z0-9_]*)(\.[A-Za-z_][A-Za-z0-9_]*)?', args[1]):
                raise Unsupported('temporal accessor on an expression')
            return f'{args[1]}.{unit[1]}'
        if name in self.SPELLING:
            spelled = self.SPELLING[name]
            if spelled is None:
                raise Unsupported(f'function {name} for Neo4j')
            return f'{spelled}(' + ', '.join(args) + ')'
        return f'{name.lower()}(' + ', '.join(args) + ')'

    def function(self, name, args, e=None):
        if name == 'EQUALS' and e is not None:
            label = self._typed_label(e)
            if label:
                return label
        return super().function(name, args, e)

    def _typed_label_of(self, e):
        """(owner uid, Neo4j label) when `e` is `x.<discriminator> = 'value'`
        of a typed label (e.g. PLACE.type = 'City' -> City)."""
        if e.get('function') != 'EQUALS':
            return None
        left, right = (e.get('children') or [None, None])[:2]
        if not left or not right or left.get('k') != 'PROPERTY' or right.get('k') != 'LITERAL':
            return None
        value = (right.get('value') or {}).get('text')
        tables = self.entity_tables.get(left.get('owner')) or []
        if len(tables) != 1 or (right.get('value') or {}).get('type') != 'STRING':
            return None
        label = self.typed.get((tables[0].upper(), left.get('property'), value))
        return (left.get('owner'), label) if label else None

    def _typed_label(self, e):
        found = self._typed_label_of(e)
        if found is None:
            return None
        owner = self.printer_names.get(found[0])
        return f'({self.ident(owner)}:{self.ident(found[1])})' if owner else None

    def fold_node_labels(self, graphs, predicate, declared):
        """`(x:Place) ... WHERE x:City`  ->  `(x:City)`.  The mapping already
        reads `(:City)` as PLACE with type 'City' (the frontend's translation
        of the input), so both spellings denote the same nodes; Neo4j however estimates a node with two labels by
        multiplying their selectivities, which made it plan bi-3 around a
        near-empty estimate (Apply per message, 1.9s -> 103s).  Only nodes this
        clause declares are folded, one typed label per node."""
        if predicate is None:
            return {}, predicate
        fresh = {n['uid'] for g in graphs or [] for n in g.get('nodes') or [] if n['uid'] not in declared}
        folds, rest = {}, []
        for conjunct in _conjuncts(predicate):
            found = self._typed_label_of(conjunct)
            if found and found[0] in fresh and found[0] not in folds:
                folds[found[0]] = found[1]
            else:
                rest.append(conjunct)
        if not folds:
            return {}, predicate
        if not rest:
            return folds, None
        if len(rest) == 1:
            return folds, rest[0]
        return folds, {**predicate, 'children': rest}


def neo4j_profile(mapping=None):
    from .profiles import Profile
    path = mapping or Path(__file__).resolve().parents[1] / 'profiles/ldbc-snb-neo4j.yaml'
    m = load_mapping(path)
    return Profile('neo4j', Neo4jDialect(m), Neo4jFrontend(m))
