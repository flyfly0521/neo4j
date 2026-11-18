"""A profile = how the middleware talks to one target system:
the output dialect (printer hooks) + the input frontend (to internal Cypher)."""
from dataclasses import dataclass

from .dialects import get_dialect
from .frontends import IdentityFrontend, KuzuFrontend


@dataclass
class Profile:
    name: str
    dialect: object
    frontend: object


def _neug():
    return Profile('neug', get_dialect('neug'), IdentityFrontend())


def _kuzu():
    return Profile('kuzu', get_dialect('kuzu'), KuzuFrontend())


def _neo4j(mapping=None):
    from .neo4j import neo4j_profile
    return neo4j_profile(mapping)


PROFILES = {'neug': _neug, 'kuzu': _kuzu, 'neo4j': _neo4j}


def register(name, factory):
    PROFILES[name] = factory


def get_profile(name, **options):
    try:
        return PROFILES[name](**options)
    except KeyError:
        raise ValueError(f'Unknown profile {name!r}; known: {sorted(PROFILES)}') from None
