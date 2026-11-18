"""Declared integrity constraints of the target data model.

They are PREMISES of individual rewrite steps, never of the QueryPart
theorem itself: a step that relies on one records it (Result.premises), and
the equivalence it proves holds on every database that satisfies the
declared constraints.  Each constraint therefore comes with a Cypher check
that counts its violations on the target database; run them (via
IndexMiddleware.constraint_checks()) before trusting rewrites that use it.

Profile syntax (Neo4j names), e.g.

  constraints:
    - {kind: at_most_one, rel: REPLY_OF, from: Message}   # <= 1 outgoing per Message
    - {kind: at_most_one, rel: CONTAINER_OF, to: Post}    # <= 1 incoming per Post
    - {kind: endpoint_labels, rel: IS_LOCATED_IN, from: Person, to: City}
    - {kind: unique_property, label: Tag, property: name}

Kinds
  at_most_one      every `from` node has at most one outgoing `rel`
                   (or: every `to` node has at most one incoming `rel`)
  at_least_one     every `from` node has at least one outgoing `rel`
                   (or: every `to` node has at least one incoming `rel`)
  endpoint_labels  every `rel` leaving a `from` node ends at a `to` node
  unique_property  no two `label` nodes share a non-null `property` value
  required_property  every `label` node has a non-null `property`
"""
from dataclasses import dataclass

KINDS = {'at_most_one', 'at_least_one', 'endpoint_labels', 'unique_property', 'required_property'}


@dataclass(frozen=True)
class Constraint:
    kind: str
    rel: str = ''
    src: str = ''
    dst: str = ''
    label: str = ''
    prop: str = ''

    def describe(self):
        if self.kind in ('at_most_one', 'at_least_one'):
            return (f'{self.kind}: (:{self.src})-[:{self.rel}]->' if self.src
                    else f'{self.kind}: -[:{self.rel}]->(:{self.dst})')
        if self.kind == 'endpoint_labels':
            return f'endpoint_labels: (:{self.src})-[:{self.rel}]->(:{self.dst})'
        return f'{self.kind}: {self.label}.{self.prop}'

    def check(self):
        """Neo4j Cypher returning one row {violations: n}; n must be 0."""
        if self.kind == 'at_most_one' and self.src:
            return (f'MATCH (n:`{self.src}`)-[r:`{self.rel}`]->() WITH n, count(r) AS c '
                    f'WHERE c > 1 RETURN count(n) AS violations')
        if self.kind == 'at_most_one':
            return (f'MATCH ()-[r:`{self.rel}`]->(n:`{self.dst}`) WITH n, count(r) AS c '
                    f'WHERE c > 1 RETURN count(n) AS violations')
        if self.kind == 'at_least_one' and self.src:
            return (f'MATCH (n:`{self.src}`) WHERE NOT EXISTS {{ (n)-[:`{self.rel}`]->() }} '
                    f'RETURN count(n) AS violations')
        if self.kind == 'at_least_one':
            return (f'MATCH (n:`{self.dst}`) WHERE NOT EXISTS {{ ()-[:`{self.rel}`]->(n) }} '
                    f'RETURN count(n) AS violations')
        if self.kind == 'endpoint_labels':
            return (f'MATCH (:`{self.src}`)-[r:`{self.rel}`]->(x) WHERE NOT x:`{self.dst}` '
                    f'RETURN count(r) AS violations')
        if self.kind == 'required_property':
            return f'MATCH (n:`{self.label}`) WHERE n.`{self.prop}` IS NULL RETURN count(n) AS violations'
        return (f'MATCH (n:`{self.label}`) WHERE n.`{self.prop}` IS NOT NULL '
                f'WITH n.`{self.prop}` AS v, count(n) AS c WHERE c > 1 RETURN count(v) AS violations')


class Constraints:
    def __init__(self, entries, labels=None, typed=()):
        self.items = []
        for entry in entries or []:
            kind = entry.get('kind')
            if kind not in KINDS:
                raise ValueError(f'unknown constraint kind {kind!r}')
            item = Constraint(kind, entry.get('rel', ''), entry.get('from', ''), entry.get('to', ''),
                              entry.get('label', ''), entry.get('property', ''))
            if kind in ('at_most_one', 'at_least_one') and bool(item.src) == bool(item.dst):
                raise ValueError(f'{kind} needs exactly one of from/to: {entry}')
            if kind == 'endpoint_labels' and not (item.rel and item.src and item.dst):
                raise ValueError(f'endpoint_labels needs rel, from and to: {entry}')
            if kind in ('unique_property', 'required_property') and not (item.label and item.prop):
                raise ValueError(f'{kind} needs label and property: {entry}')
            self.items.append(item)
        self.labels = labels or {}   # Neo4j label -> internal tables (for coverage tests)
        self.typed = set(typed)      # labels that are a predicate on a table (City on PLACE)

    def _covers(self, label, tables, known=()):
        """Does every node with internal tables `tables` (and the labels
        `known` to hold for it) carry `label`?  A typed label (City) is a
        predicate on its table, so table membership never implies it; it must
        be known, i.e. declared on the node or derived from endpoint_labels."""
        if label in known:
            return True
        if label in self.typed:
            return False
        own = {t.upper() for t in self.labels.get(label, [label])}
        return bool(tables) and {t.upper() for t in tables} <= own

    def at_most_one_out(self, rel, tables, known=()):
        return next((c for c in self.items if c.kind == 'at_most_one' and c.rel == rel and c.src
                     and self._covers(c.src, tables, known)), None)

    def at_most_one_in(self, rel, tables, known=()):
        return next((c for c in self.items if c.kind == 'at_most_one' and c.rel == rel and c.dst
                     and self._covers(c.dst, tables, known)), None)

    def at_least_one_out(self, rel, tables, known=()):
        return next((c for c in self.items if c.kind == 'at_least_one' and c.rel == rel and c.src
                     and self._covers(c.src, tables, known)), None)

    def at_least_one_in(self, rel, tables, known=()):
        return next((c for c in self.items if c.kind == 'at_least_one' and c.rel == rel and c.dst
                     and self._covers(c.dst, tables, known)), None)

    def implied_endpoint(self, rel, known):
        """Constraints `(:X)-[:rel]->(:Y)` with X in the known labels of the source."""
        return [c for c in self.items if c.kind == 'endpoint_labels' and c.rel == rel and c.src in known]

    def unique(self, label, prop):
        return next((c for c in self.items if c.kind == 'unique_property'
                     and c.label == label and c.prop == prop), None)

    def key(self, label, prop=None):
        """(property, unique, required) of a declared key of `label`: a
        property that is unique and never null, so it identifies the node."""
        for u in self.items:
            if u.kind == 'unique_property' and u.label == label and (prop is None or u.prop == prop):
                r = next((c for c in self.items if c.kind == 'required_property'
                          and c.label == label and c.prop == u.prop), None)
                if r is not None:
                    return u.prop, u, r
        return None

    def checks(self):
        return [dict(constraint=c.describe(), cypher=c.check()) for c in self.items]
