"""Balanced syntax spans shared by conservative local rewrite rules.

These helpers preserve token spelling. They do not infer variable identity
from spelling across scopes; callers must obtain the input schema by binding.
"""
from .errors import Unsupported


def closing(tokens, opening):
    pairs = {'(': ')', '[': ']', '{': '}'}
    stack = []
    for i in range(opening, len(tokens)):
        word = tokens[i].text
        if word in pairs:
            stack.append(pairs[word])
        elif word in pairs.values():
            if not stack or stack.pop() != word:
                raise Unsupported('unbalanced expression')
            if not stack:
                return i
    raise Unsupported('unclosed expression')


def split_top(tokens, separator=','):
    """Split only outside nested calls, patterns, records and lists."""
    out, start, i = [], 0, 0
    while i < len(tokens):
        if tokens[i].text in ('(', '[', '{'):
            i = closing(tokens, i) + 1
            continue
        if tokens[i].text == separator:
            out.append(tokens[start:i])
            start = i + 1
        i += 1
    out.append(tokens[start:])
    return out


def name(token):
    return token.text[1:-1].replace('``', '`') if token.kind == 'quoted' else token.text


def parameter_names(tokens):
    out = set()
    for token in tokens:
        if token.kind == 'param':
            text = token.text[1:]
            out.add(text[1:-1].replace('``', '`') if text.startswith('`') else text)
    return out


def reference_positions(tokens):
    """Value positions, distinguishing map values from labels after a colon."""
    stack = []
    for i, token in enumerate(tokens):
        if token.text in ('(', '[', '{'):
            stack.append(token.text)
        elif token.text in (')', ']', '}') and stack:
            stack.pop()
        if token.kind not in ('ident', 'quoted'):
            continue
        prev = tokens[i - 1].text if i else None
        nxt = tokens[i + 1].text if i + 1 < len(tokens) else None
        map_value = prev == ':' and stack and stack[-1] == '{' and i >= 3 and tokens[i - 3].text in ('{', ',')
        map_key = nxt == ':' and stack and stack[-1] == '{' and prev in ('{', ',')
        if prev in ('.', 'AS') or prev == ':' and not map_value or nxt == '(' or map_key:
            continue
        yield i


def references(tokens, candidates):
    """Value references to known columns, excluding fields, labels and calls."""
    return {name(tokens[i]) for i in reference_positions(tokens) if name(tokens[i]) in candidates}
