"""Input frontends: translate a source system's Cypher into the internal
(NeuG) dialect the engine binds.  The identity frontend serves NeuG and Kuzu,
whose grammar and LDBC schema match NeuG's."""


class IdentityFrontend:
    name = 'identity'

    def to_internal(self, cypher):
        return cypher

    def parameter_types(self, types):
        return dict(types or {})

    def fill_to_internal(self, fill_text):
        return fill_text

    def ddl_to_internal(self, statement):
        return statement

    def table_to_internal(self, name):
        return name


class KuzuFrontend(IdentityFrontend):
    """Kuzu Cypher -> internal: same grammar family as NeuG; only spellings
    the internal binder lacks are normalized (see normalize.py)."""
    name = 'kuzu'

    def to_internal(self, cypher):
        from .neo4j import render, tokenize
        from .normalize import entity_id_comparisons, rewrite_coalesce
        from .statements import strip_comments
        tokens = tokenize(strip_comments(cypher).strip().rstrip(';'))
        return render(entity_id_comparisons(rewrite_coalesce(tokens)))
