"""Rewrite read-only CALL bodies with the parent's typed imported binding.

CALL/UNION remain in the target dialect. Only a body proved equivalent for
its imported binding is replaced; procedures and writes stay opaque.
"""
from . import queryparts
from .errors import Unsupported
from .neo4j import tokenize, render
from .engine import EngineError
from .syntax import parameter_names


def _finite_call(mw, prefix, names, body, types, inline):
    """Specialize a proved finite input domain, with exclusive runtime guards.

    The graph matcher still checks every candidate with the instantiated
    input. A guard is outside an inner CALL so an inactive branch cannot
    produce the zero row of a global aggregate. Duplicate outer input rows
    are never deduplicated or expanded here.
    """
    from .binding_values import finite_inputs, substitute_imports, equal_guard
    cases = finite_inputs(prefix, names)
    if not cases:
        return None
    outputs = queryparts.projection_outputs(render(body))
    if not outputs or len(set(outputs)) != len(outputs) or set(outputs) & set(names):
        return None
    # The substitution guards use typed scalar representations. Maps, lists,
    # graph values and unknown parameter types need another contract.
    allowed = {'BOOL', 'INT64', 'INT32', 'INT16', 'INT8', 'INT128', 'UINT64',
               'DOUBLE', 'FLOAT', 'STRING', 'DATE', 'TIMESTAMP'}
    for values in cases:
        for expression in values.values():
            if expression.upper() == 'NULL':
                continue
            probe = mw._to_internal('RETURN ' + expression + ' AS cico_origin', inline)
            used = {k: v for k, v in types.items() if k in parameter_names(tokenize(probe))}
            columns = mw.engine.bind(probe, used)['columns']
            if len(columns) != 1 or columns[0][1] not in allowed:
                raise Unsupported('finite CALL origin is not a proved primitive scalar')
    results, indexes, proofs = [], set(), []
    for values in cases:
        instantiated = substitute_imports(body, values)
        used = {k: v for k, v in types.items() if k in parameter_names(tokenize(instantiated))}
        result = mw._rewrite_query(instantiated, used, inline)
        if result.rewritten:
            if queryparts.projection_outputs(result.cypher) != outputs:
                raise Unsupported('finite CALL changed output column names')
            indexes.update(result.indexes)
            proofs.extend(result.proofs)
        results.append(result)
    if not indexes:
        return None
    d = mw.profile.dialect
    imports = ', '.join(d.ident(n) for n in names)
    returned = ', '.join(d.ident(n) for n in outputs)
    previous, branches = [], []
    for values, result in zip(cases, results):
        guard = ' AND '.join(equal_guard(n, values[n], d) for n in names)
        exclusive = '(' + guard + ')'
        if previous:
            exclusive += ' AND NOT (' + ' OR '.join(previous) + ')'
        previous.append('(' + guard + ')')
        local = result.cypher if result.rewritten else render(body)
        branches.append('WITH ' + imports + ' WITH ' + imports + ' WHERE ' + exclusive +
                        ' CALL { WITH ' + imports + ' ' + local + ' } RETURN ' + returned)
    proofs.append(dict(rule='finite_call_inputs', imports=names, cases=len(cases),
                       checks=['correlated_record_fields', 'closed_scalar_origins',
                               'null_safe_exclusive_guards', 'typed_scalar_identity_guards',
                               'outer_row_multiplicity_preserved',
                               'inactive_aggregate_branch_suppressed', 'output_names_unchanged']))
    return ' UNION ALL '.join(branches), sorted(indexes), proofs


def branches(tokens):
    depth, start, out = 0, 0, []
    for i, token in enumerate(tokens):
        if depth == 0 and token.upper == 'UNION' and token.kind == 'ident':
            out.append((start, i))
            start = i + 1
            if start < len(tokens) and tokens[start].upper == 'ALL':
                start += 1
        depth += token.text in ('(', '[', '{')
        depth -= token.text in (')', ']', '}')
    out.append((start, len(tokens)))
    return out


def rewrite_calls(mw, cypher, types, inline):
    tokens = tokenize(cypher)
    edits, indexes, notes, proofs = [], set(), [], []
    depth, i = 0, 0
    while i < len(tokens):
        t = tokens[i]
        if depth != 0 or t.kind != 'ident' or t.upper != 'CALL':
            depth += t.text in ('(', '[', '{')
            depth -= t.text in (')', ']', '}')
            i += 1
            continue
        start = i
        i += 1
        imported = None
        if i < len(tokens) and tokens[i].text == '(':
            end = i + 1
            while end < len(tokens) and tokens[end].text != ')':
                end += 1
            scope = tokens[i + 1:end]
            if all(x.text == ',' or x.kind in ('ident', 'quoted') for x in scope):
                imported = [x.text for x in scope if x.text != ',']
            i = end + 1
        if i >= len(tokens) or tokens[i].text != '{':
            continue  # opaque procedure call
        opening = i
        level, i = 1, i + 1
        while i < len(tokens) and level:
            level += tokens[i].text == '{'
            level -= tokens[i].text == '}'
            i += 1
        if level:
            return None, [], ['unclosed CALL'], []
        closing = i - 1
        # Batched writes and optional CALL need separate semantics.
        if (i < len(tokens) and tokens[i].upper == 'IN') or (start and tokens[start - 1].upper == 'OPTIONAL'):
            continue
        for lo, hi in branches(tokens[opening + 1:closing]):
            lo += opening + 1
            hi += opening + 1
            body = tokens[lo:hi]
            names = imported
            legacy = 0
            if names is None:
                if body and body[0].upper == 'WITH':
                    clauses = queryparts._top_level(body)
                    legacy = clauses[1][0] if len(clauses) > 1 else len(body)
                    scope = body[1:legacy]
                    if not all(x.text == ',' or x.kind in ('ident', 'quoted') for x in scope):
                        notes.append('unsupported importing WITH')
                        continue
                    names = [x.text for x in scope if x.text != ',']
                else:
                    names = []
            try:
                if names:
                    try:
                        finite = _finite_call(mw, render(tokens[:start]), names, body[legacy:], types, inline)
                    except (Unsupported, EngineError) as error:
                        notes.append('finite CALL: ' + str(error)[:160])
                        finite = None
                    if finite:
                        text, used, evidence = finite
                        edits.append((lo, hi, text))
                        indexes.update(used)
                        proofs.extend(evidence)
                        continue
                stub, stub_types = '', {}
                if names:
                    prefix = render(tokens[:start]) + ' RETURN ' + ', '.join(names)
                    columns = mw._bind_prefix(prefix, types, inline)['columns']
                    reserved = set(types) | set(inline or {}) | parameter_names(body)
                    stub, stub_types = queryparts.stub(columns, mw.profile.dialect, names, reserved)
                local = list(body[legacy:])
                # Imported CALL variables are global to its body in Neo4j;
                # carry them through WITH in the internal lexical scope.
                clauses = queryparts._top_level(local)
                for k in range(len(clauses) - 1, -1, -1):
                    pos, word = clauses[k]
                    if word != 'WITH':
                        continue
                    end = clauses[k + 1][0] if k + 1 < len(clauses) else len(local)
                    for j in range(pos + 1, end):
                        if local[j].upper in queryparts.PROJECTION_TAIL:
                            end = j
                            break
                    outputs = queryparts.projection_outputs(render(local[pos:end]))
                    if outputs is None:
                        raise Unsupported('CALL WITH output names unavailable')
                    missing = [name for name in names if name not in outputs]
                    if missing:
                        local[end:end] = tokenize(', ' + ', '.join(missing) + ' ')
                source = render(local)
                probe = (stub + ' ' if stub else '') + source
                used = {k: v for k, v in {**types, **stub_types}.items()
                        if k in parameter_names(tokenize(probe))}
                result = mw._rewrite_query(probe, used, inline)
                if not result.rewritten:
                    notes.append(result.reason + ': ' + result.detail[:150])
                    continue
                rewritten = tokenize(result.cypher)
                head = tokenize(stub)
                if head:
                    if [x.text for x in rewritten[:len(head)]] != [x.text for x in head]:
                        notes.append('CALL input stub changed')
                        continue
                    rewritten = rewritten[len(head):]
                if parameter_names(rewritten) & set(stub_types):
                    notes.append('CALL input stub leaked')
                    continue
                edits.append((lo + legacy, hi, render(rewritten)))
                indexes.update(result.indexes)
                proofs.extend(result.proofs)
            except (Unsupported, EngineError) as error:
                notes.append(str(error)[:200])
    for lo, hi, source in reversed(edits):
        tokens[lo:hi] = tokenize(' ' + source + ' ')
    return (render(tokens) if edits else None), sorted(indexes), notes, proofs
