from .base import Dialect
from .neug import KuzuDialect, NeugDialect

DIALECTS = {'neug': NeugDialect, 'kuzu': KuzuDialect}


def register(name, factory):
    DIALECTS[name] = factory


def get_dialect(name, **options):
    try:
        return DIALECTS[name](**options)
    except KeyError:
        raise ValueError(f'Unknown dialect {name!r}; known: {sorted(DIALECTS)}') from None
