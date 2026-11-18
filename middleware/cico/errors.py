class Unsupported(Exception):
    """The Bound IR contains a construct this printer/dialect cannot express
    faithfully.  Callers must fall back to the original query."""
