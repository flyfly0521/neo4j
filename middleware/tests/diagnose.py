"""Show RewriterV4's search outcome for each query (matches found vs cost choice)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cico import IndexMiddleware
from bi_coverage import fill_placeholders, query_name
schema, stats, script = sys.argv[1:4]
files = sys.argv[4:]
with IndexMiddleware(schema, stats if stats != '-' else None) as mw:
    mw.execute_script(Path(script).read_text())
    for f in files:
        text = fill_placeholders(Path(f).read_text())
        try:
            r = mw.engine.rewrite(text)
        except Exception as e:
            print(query_name(f), 'ERROR', str(e)[:120]); continue
        print(f"{query_name(f):7} changed={r.get('changed')} compared={r['cost_compared']} alts={r['alternatives']} "
              f"orig={r['original_work']} sel={r['selected_work']} steps={len(r.get('index_entries') or [])}")
