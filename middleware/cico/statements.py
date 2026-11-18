"""Split Cypher scripts and classify statements sent to the middleware."""
import re
from dataclasses import dataclass

_IDENT = r'(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)'


def strip_comments(text):
    """Remove // and /* */ comments outside string literals."""
    out, i, n, quote = [], 0, len(text), None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            out.append(ch)
            i += 1
        elif text.startswith('//', i):
            while i < n and text[i] != '\n':
                i += 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def split_script(text):
    """Split a script on ';' outside strings/comments; drop empty statements."""
    text = strip_comments(text)
    statements, current, quote = [], [], None
    i = 0
    while i < len(text):
        ch = text[i]
        current.append(ch)
        if quote:
            if ch == '\\' and i + 1 < len(text):
                current.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"', '`'):
            quote = ch
        elif ch == ';':
            current.pop()
            statements.append(''.join(current).strip())
            current = []
        i += 1
    statements.append(''.join(current).strip())
    return [s for s in statements if s]


@dataclass
class Fill:
    table: str
    columns: list
    source: str
    text: str


def parse_fill(statement):
    s = strip_comments(statement).strip().rstrip(';').strip()
    m = re.fullmatch(rf'FILL\s+({_IDENT})\s*\(([^)]*)\)\s*FROM\s*\((.*)\)', s, re.I | re.S)
    if not m:
        return None
    table = m[1][1:-1] if m[1].startswith('`') else m[1]
    columns = [c.strip().strip('`') for c in m[2].split(',') if c.strip()]
    return Fill(table=table, columns=columns, source=m[3].strip(), text=s)


DDL = re.compile(r'^\s*(CREATE\s+(NODE|REL)\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b', re.I)
NOOP = re.compile(r'^\s*(CHECKPOINT|BEGIN\s+TRANSACTION|COMMIT|ROLLBACK)\s*$', re.I)


def classify(statement):
    """'fill' | 'ddl' | 'noop' | 'query'."""
    s = strip_comments(statement).strip()
    if not s.rstrip(';').strip():
        return 'noop'  # comments only
    if re.match(r'^FILL\b', s, re.I):
        return 'fill'
    if DDL.match(s):
        return 'ddl'
    if NOOP.match(s.rstrip(';')):
        return 'noop'
    return 'query'
