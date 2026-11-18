from .base import Dialect


class NeugDialect(Dialect):
    """NeuG Cypher: `(n:A:B)` is a label UNION, as in the binder."""
    name = 'neug'


class KuzuDialect(Dialect):
    """Kuzu shares NeuG's grammar family (NeuG's compiler derives from Kuzu):
    label unions `(n:A:B)`, `label(x)`, `CAST(x AS T)`.  Differences are
    confined to spellings overridden here."""
    name = 'kuzu'

    UNBOUNDED = 4294967295

    def var_length(self, lower, upper):
        # Kuzu rejects upper bounds above var_length_extend_max_depth (default
        # 30) and reads an open `*l..` as l..max.  The Kuzu frontend therefore
        # equates `*l..` with NeuG's unbounded walk; this is exact while no
        # path is longer than Kuzu's cap (e.g. LDBC reply trees).
        return f'*{lower}..' if upper == self.UNBOUNDED else f'*{lower}..{upper}'
