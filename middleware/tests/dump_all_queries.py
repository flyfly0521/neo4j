"""Every docs query of one system/workload through all middleware stages, as JSON lines:
original -> internal (frontend) -> NeuG surface check -> per-QueryPart probes and
their rewritten internal text -> output.  Used for the per-query document and to
check the rule "a converted QueryPart binds in NeuG".

    python3 tests/dump_all_queries.py SYSTEM WORKLOAD > out.jsonl
"""
import json, sys, traceback
from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE), str(_HERE.parent)]
import workloads as W
from cico import IndexMiddleware
from cico.printer import Printer
from workload_coverage import profile_for

system, workload = sys.argv[1], sys.argv[2]
with IndexMiddleware(W.schema_path(workload), profile=profile_for(system, workload)) as mw:
    fills = [dict(ok=r.ok, kind=r.kind, reason=r.reason, detail=(r.detail or '')[:300], cypher=r.cypher)
             for r in mw.execute_script(W.index_script(workload))]
    print(json.dumps(dict(type='fills', system=system, workload=workload, fills=fills), ensure_ascii=False), flush=True)
    neug = Printer('neug')
    neug.dialect.set_schema(mw.schema)  # print relationship groups, not child tables
    state = {'part': None, 'calls': []}
    orig_rewrite = mw.engine.rewrite

    def spy(internal, types):
        resp = orig_rewrite(internal, types)
        rec = {'part': state['part'], 'internal_in': internal, 'changed': bool(resp.get('changed'))}
        if resp.get('changed'):
            try:
                rec['internal_out'] = neug.print(resp['ir']).cypher
            except Exception as e:
                rec['internal_out'] = f'<print failed: {e}>'
        state['calls'].append(rec)
        return resp

    orig_part = mw._rewrite_part

    def part_spy(parts, i, types, inline):
        state['part'] = i
        try:
            return orig_part(parts, i, types, inline)
        finally:
            state['part'] = None

    mw.engine.rewrite = spy
    mw._rewrite_part = part_spy
    for path in W.query_files(workload, system):
        text = Path(path).read_text()
        rec = dict(type='query', system=system, workload=workload, name=W.query_name(path),
                   file=W.docs_file(path), overlay=path if path != W.docs_file(path) else None, text=text)
        if path != W.docs_file(path):
            rec['docs_text'] = Path(W.docs_file(path)).read_text()
        if W.is_write(text):
            rec['skipped'] = 'write'
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            continue
        try:
            rec['internal'] = mw.frontend.to_internal(text)
        except Exception as e:
            rec['internal_error'] = f'{type(e).__name__}: {e}'
        try:
            mw._to_internal(text)
        except Exception as e:
            rec['whole_check'] = f'{type(e).__name__}: {e}'
        try:
            params = W.parameters(workload, path, text, 0)
        except Exception as e:
            params, rec['param_error'] = {}, str(e)[:200]
        state['calls'] = []
        try:
            r = mw.execute(text, parameters=params)
            rec.update(rewritten=r.rewritten, reason=r.reason, detail=(r.detail or '')[:1500], parts=r.parts,
                       indexes=r.indexes, specialized=r.specialized, output=r.cypher)
        except Exception:
            rec['crash'] = traceback.format_exc()[-800:]
        rec['probes'] = state['calls']
        print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)
