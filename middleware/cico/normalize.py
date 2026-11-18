"""Token-level normalizations shared by the input frontends.

They rewrite spellings the internal binder (NeuG) lacks into equivalent
internal forms; every rewrite is an identity in Cypher semantics:

  coalesce(a, b, ..., z)        CASE WHEN a IS NOT NULL THEN a WHEN ... ELSE z END
  a < b <= c   (Neo4j chains)   a < b AND b <= c
  CAST(<int> AS STRING) + 's'   's'-prefixed string literal (constant folding,
                                e.g. after parameter specialization)
"""
import re

from .errors import Unsupported

COMPARISONS = {'<', '<=', '>', '>='}


def _tokens():
    from .neo4j import tokenize, render, Tok
    return tokenize, render, Tok


def call_args(tokens, open_index):
    """(end index after ')', [argument token lists]) for a call at '('."""
    depth, j, args, current = 0, open_index, [], []
    while j < len(tokens):
        t = tokens[j]
        if t.text in ('(', '[', '{'):
            depth += 1
            if depth > 1:
                current.append(t)
        elif t.text in (')', ']', '}'):
            depth -= 1
            if depth == 0:
                args.append(current)
                return j + 1, [a for a in args if a]
            current.append(t)
        elif t.text == ',' and depth == 1:
            args.append(current)
            current = []
        else:
            current.append(t)
        j += 1
    raise Unsupported('unbalanced parentheses')


def rewrite_coalesce(tokens, translate=None):
    """Replace coalesce(...) calls (recursively, args via `translate`)."""
    tokenize, render, _ = _tokens()
    translate = translate or (lambda ts: ts)
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == 'ident' and t.text.lower() == 'coalesce' and i + 1 < len(tokens) and tokens[i + 1].text == '(' \
                and not (out and out[-1].text == '.'):
            end, args = call_args(tokens, i + 1)
            texts = [render(translate(rewrite_coalesce(a, translate))).strip() for a in args]
            if len(texts) == 1:
                text = f'({texts[0]})'
            else:
                whens = ' '.join(f'WHEN ({a}) IS NOT NULL THEN ({a})' for a in texts[:-1])
                text = f'(CASE {whens} ELSE ({texts[-1]}) END)'
            new = tokenize(text)
            new[0].ws = t.ws
            out.extend(new)
            i = end
            continue
        out.append(t)
        i += 1
    return out


def _term_end(tokens, i):
    """End of a simple operand starting at i: $p, literal, a.b.c, f(...)."""
    n = len(tokens)
    if i >= n:
        return None
    t = tokens[i]
    if t.kind in ('param', 'number', 'string'):
        j = i + 1
    elif t.kind in ('ident', 'quoted'):
        j = i + 1
        if j < n and tokens[j].text == '(':
            j, _ = call_args(tokens, j)
    else:
        return None
    while j + 1 < n and tokens[j].text == '.' and tokens[j + 1].kind in ('ident', 'quoted'):
        j += 2
    return j


def _term_start(tokens, end):
    """Start of a simple operand ending just before `end`."""
    j = end - 1
    if j < 0:
        return None
    if tokens[j].text == ')':
        depth, k = 0, j
        while k >= 0:
            depth += tokens[k].text == ')'
            depth -= tokens[k].text == '('
            if depth == 0:
                break
            k -= 1
        j = k - 1 if k - 1 >= 0 and tokens[k - 1].kind in ('ident', 'quoted') else None
        if j is None:
            return None
    while j >= 2 and tokens[j - 1].text == '.' and tokens[j - 2].kind in ('ident', 'quoted', 'param'):
        j -= 2
    if tokens[j].kind not in ('param', 'number', 'string', 'ident', 'quoted'):
        return None
    return j


def split_chained_comparisons(tokens):
    """a OP1 b OP2 c  (OP in < <= > >=) -> a OP1 b AND b OP2 c."""
    tokenize, render, Tok = _tokens()
    out, i, n = list(tokens), 0, len(tokens)
    changed = True
    while changed:
        changed = False
        n = len(out)
        for i in range(n):
            if out[i].text not in COMPARISONS:
                continue
            b_start = i + 1
            b_end = _term_end(out, b_start)
            if b_end is None or b_end >= n or out[b_end].text not in COMPARISONS:
                continue
            if _term_start(out, i) is None:
                continue
            middle = [Tok(t.kind, t.text, t.ws) for t in out[b_start:b_end]]
            middle[0].ws = ' '
            out[b_end:b_end] = [Tok('ident', 'AND', ' ')] + middle
            changed = True
            break
    return out


_FOLD = re.compile(r"CAST\(\s*(-?\d+)\s+AS\s+STRING\s*\)\s*\+\s*'([^'\\]*)'", re.I)


def fold_constants(text):
    """CAST(4 AS STRING) + ' hours'  ->  '4 hours'   (string literal concat)."""
    return _FOLD.sub(lambda m: f"'{m[1]}{m[2]}'", text)


def pattern_variables(tokens):
    """Variables declared by node or relationship patterns of a statement."""
    found = set()
    for i in range(len(tokens) - 2):
        t, a, b = tokens[i], tokens[i + 1], tokens[i + 2]
        if t.text == '(' and a.kind in ('ident', 'quoted') and b.text in (':', ')', '{'):
            found.add(a.text)
        if t.text == '[' and i and tokens[i - 1].text in ('-', '<-') and a.kind in ('ident', 'quoted') \
                and b.text in (':', ']', '*', '{'):
            found.add(a.text)
    return found


def declared_labels(tokens):
    """variable -> frozenset of the tables its pattern declarations name
    (None when declarations disagree or name none)."""
    seen = {}
    for i in range(len(tokens) - 1):
        t, a = tokens[i], tokens[i + 1]
        if t.text not in ('(', '[') or a.kind not in ('ident', 'quoted'):
            continue
        names, j = [], i + 2
        while j + 1 < len(tokens) and tokens[j].text in (':', '|') and tokens[j + 1].kind in ('ident', 'quoted'):
            names.append(tokens[j + 1].text.upper())
            j += 2
        if names:
            seen.setdefault(a.text, set()).add(frozenset(names))
    return {v: next(iter(s)) if len(s) == 1 else None for v, s in seen.items()}


def entity_id_comparisons(tokens, multi_pair=frozenset()):
    """`a = b` / `a <> b` between graph entities  ->  `id(a) = id(b)`.

    Identical in Cypher semantics (entities are equal iff their identities
    are); NeuG's binder asserts (types.cpp) when it compares two entities
    bound to different table sets, e.g. `NOT person = friend` with an
    unlabelled `friend`, while comparing internal ids always binds."""
    _, _, Tok = _tokens()
    variables = pattern_variables(tokens)
    if not variables:
        return tokens
    labels = declared_labels(tokens)
    # A relationship group with several endpoint pairs binds to different
    # child tables per pattern, so equal names do not mean equal tables.
    labels = {v: (None if s is not None and s & {m.upper() for m in multi_pair} else s) for v, s in labels.items()}

    def bare(k):
        if not (0 <= k < len(tokens)) or tokens[k].kind not in ('ident', 'quoted') or tokens[k].text not in variables:
            return False
        before = tokens[k - 1].text if k else ''
        after = tokens[k + 1].text if k + 1 < len(tokens) else ''
        return before not in ('.', ':', '(', '[') and after not in ('.', '(', ':', '{')

    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        same = (labels.get(tokens[i - 1].text) is not None and i + 1 < len(tokens)
                and labels.get(tokens[i - 1].text) == labels.get(tokens[i + 1].text)) if i else False
        if t.text in ('=', '<>') and bare(i - 1) and bare(i + 1) and out and not same:
            left = out.pop()
            right = tokens[i + 1]
            out += [Tok('ident', 'id', left.ws), Tok('punct', '('), Tok(left.kind, left.text), Tok('punct', ')'),
                    Tok('punct', t.text, ' '), Tok('ident', 'id', ' '), Tok('punct', '('),
                    Tok(right.kind, right.text), Tok('punct', ')')]
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def normalize_temporal_comparisons(text):
    """Give equivalent literal/property inequalities one shape for matching."""
    tokenize, render, Tok = _tokens()
    tokens = tokenize(text)
    inverse = {'<': '>', '<=': '>=', '>': '<', '>=': '<='}
    edits = []
    for i, token in enumerate(tokens):
        if token.upper not in ('TIMESTAMP', 'DATE') or token.kind != 'ident':
            continue
        if i and tokens[i - 1].upper not in ('WHERE', 'AND', 'OR', '(', 'NOT'):
            continue
        j = i + 1
        if j + 2 >= len(tokens) or tokens[j].text != '(' or tokens[j + 1].kind != 'string' or tokens[j + 2].text != ')':
            continue
        j += 3
        if j + 4 < len(tokens) and tokens[j].text == '+' and tokens[j + 1].upper == 'DURATION' and tokens[j + 2].text == '(' and tokens[j + 3].kind == 'string' and tokens[j + 4].text == ')':
            j += 5
        if j + 3 >= len(tokens) or tokens[j].text not in inverse:
            continue
        prop = tokens[j + 1:j + 4]
        if not (prop[0].kind in ('ident', 'quoted') and prop[1].text == '.' and prop[2].kind in ('ident', 'quoted')):
            continue
        if j + 4 < len(tokens) and tokens[j + 4].upper not in ('AND', 'OR', ')', 'WITH', 'RETURN'):
            continue
        replacement = render(prop) + ' ' + inverse[tokens[j].text] + ' ' + render(tokens[i:j])
        edits.append((i, j + 4, tokenize(' ' + replacement)))
    for lo, hi, replacement in reversed(edits):
        tokens[lo:hi] = replacement
    return render(tokens)
