"""IncGQ-experiment-docs workloads: schema, FILL scripts, queries and parameters.

Paths default to the lab server layout and can be overridden with
CICO_DOCS (experiment docs checkout) and CICO_NEUG_DBS (NeuG databases whose
graph.yaml is the middleware schema).  Parameters are taken from the shared
manifests (`parameters/*/experiment-benchmark.csv`) and converted exactly like
the docs' Kuzu runner (`Kuzu/scripts/benchmark/run_workload.py`), falling back
to that runner's defaults for parameters a manifest does not list.
"""
import csv
import glob
import json
import os
import re
import sys
from pathlib import Path

DOCS = Path(os.environ.get('CICO_DOCS', '/mnt/sdb1/feiyang/IncGQ-experiment-docs'))
NEUG_DBS = Path(os.environ.get('CICO_NEUG_DBS', '/mnt/sdb1/feiyang/databases'))
PROFILES = Path(__file__).resolve().parents[1] / 'profiles'
# Target-system schemas (`python -m cico schema --kuzu DB`); NeuG's otherwise.
SCHEMAS = Path(os.environ.get('CICO_SCHEMAS', '/mnt/sdb1/feiyang/cico_work/schemas'))

WORKLOADS = {
    'hetionet': dict(schema='hetionet', runner='hetionet', manifest=None,
                     index=['hetionet/*/index.cypher'], mapping='hetionet-neo4j.yaml'),
    'ldbc-bi': dict(schema='bi_sf1', runner='bi-sf1', manifest='bi-sf1',
                    index=['ldbc-bi/index.cypher'], mapping='ldbc-snb-neo4j.yaml'),
    'ldbc-ic': dict(schema='ic_sf0.1', runner='ic-sf1', manifest='ic-sf1',
                    index=['ldbc-ic/*/index.cypher'], mapping='ldbc-snb-neo4j.yaml'),
    'lsqb': dict(schema='lsqb_sf1', runner='lsqb-sf1', manifest=None,
                 index=['lsqb/*/index.cypher'], mapping='lsqb-neo4j.yaml'),
    'finbench': dict(schema='finbench_sf1', runner='finbench-sf1', manifest='finbench-sf1',
                     index=['finbench/*/index.cypher'], mapping='finbench-neo4j.yaml'),
}
SYSTEM_DIRS = {'kuzu': 'Kuzu', 'neo4j': 'Neo4j', 'neug': 'NeuG'}
SKIP = re.compile(r'(setup|create-graph|drop-graph|without-precomputation|README)', re.I)


def schema_path(workload):
    target = SCHEMAS / f'{workload}.yaml'
    return target if target.is_file() else NEUG_DBS / WORKLOADS[workload]['schema'] / 'graph.yaml'


def mapping_path(workload):
    return PROFILES / WORKLOADS[workload]['mapping']


def index_script(workload):
    """All NeuG FILL/DDL scripts of a workload, concatenated in path order."""
    files = sorted({f for p in WORKLOADS[workload]['index']
                    for f in glob.glob(str(DOCS / 'NeuG/queries/index' / p))})
    return '\n'.join(Path(f).read_text().rstrip().rstrip(';') + ';' for f in files)


# Semantics-preserving edits of docs queries (implementation/middleware/query_edits,
# same relative paths).  Each edit is checked against the docs
# original on the real system (tests/equivalence_check.py); CICO_QUERY_OVERLAY=''
# disables the overlay.
OVERLAY = os.environ.get('CICO_QUERY_OVERLAY', str(Path(__file__).resolve().parents[2]
                                                   / 'implementation/middleware/query_edits'))


def docs_file(path):
    """The docs original of a (possibly overlaid) query file."""
    if OVERLAY and str(path).startswith(OVERLAY):
        return str(DOCS / Path(path).relative_to(OVERLAY))
    return str(path)


def query_files(workload, system, variant='baseline'):
    root = DOCS / SYSTEM_DIRS[system] / 'queries' / variant / workload
    files = sorted(p for p in root.rglob('*.cypher') if not SKIP.search(p.name))
    if OVERLAY:
        files = [Path(OVERLAY) / p.relative_to(DOCS) if (Path(OVERLAY) / p.relative_to(DOCS)).is_file() else p
                 for p in files]
    return [str(p) for p in files]


def query_name(path):
    p = Path(path)
    return p.parent.name if p.name in ('query.cypher', 'baseline.cypher') else p.stem


def is_write(text):
    body = re.sub(r'//[^\n]*|/\*.*?\*/', '', text, flags=re.S)
    return bool(re.search(r'\b(CREATE|MERGE|DELETE|SET|DROP|COPY|REMOVE)\b', body, re.I))


def _runner():
    sys.path.insert(0, str(DOCS / 'Kuzu/scripts/benchmark'))
    import run_workload  # noqa: E402  (docs' own parameter conversion)
    return run_workload


def _manifest_rows(name):
    path = DOCS / 'parameters' / name / 'experiment-benchmark.csv'
    rows = {}
    with open(path) as handle:
        for row in csv.DictReader(handle, delimiter='|'):
            rows.setdefault(row['query'], []).append(json.loads(row['parameters']))
    return rows


def samples(workload, path):
    """Number of manifest parameter samples for a query (1 without manifest)."""
    spec = WORKLOADS[workload]
    if not spec['manifest']:
        return 1
    rows = _manifest_rows(spec['manifest'])
    for key in _manifest_key(workload, query_name(path)):
        if key in rows:
            return len(rows[key])
    return 1


def _manifest_key(workload, name):
    if workload == 'ldbc-bi':
        m = re.search(r'bi-?(\d+)([a-z]?)', name)
        return [f'bi-{m[1]}{m[2]}', f'bi-{m[1]}a', f'bi-{m[1]}'] if m else []
    if workload == 'ldbc-ic':
        m = re.search(r'(\d+)$', name)
        return [f'interactive_{m[1]}'] if m else []
    if workload == 'finbench':
        m = re.match(r'(tcr|tsr)-(\d+)', name)
        if not m:
            return []
        number = 12 if name == 'tcr-13' else int(m[2])
        return [('complex_' if m[1] == 'tcr' else 'simple_') + str(number)]
    return []


def parameters(workload, path, text, sample=0):
    """Python values for the parameters referenced by `text` (manifest row `sample`)."""
    rw = _runner()
    spec = WORKLOADS[workload]
    values = dict(rw.default_params_for(spec['runner']))
    raw = None
    if spec['manifest']:
        rows = _manifest_rows(spec['manifest'])
        for key in _manifest_key(workload, query_name(path)):
            if key in rows:
                raw = rows[key][min(sample, len(rows[key]) - 1)]
                break
    if raw is not None:
        if workload == 'finbench':
            number = int(re.search(r'(\d+)$', _manifest_key(workload, query_name(path))[0])[1])
            ordered = [raw[k] for k in sorted(raw, key=lambda k: int(k.split('_')[1]))]
            try:
                values.update(rw.parse_finbench_row(number, ordered,
                                                    simple=query_name(path).startswith('tsr'))[0])
            except ValueError:  # the runner has no column mapping: keep its defaults
                pass
        elif workload == 'ldbc-ic':
            values.update(rw.parse_ic_row(list(raw), list(raw.values()))[0])
        else:
            for spec_key, value in raw.items():
                key, _, kind = spec_key.partition(':')
                values[key] = rw.coerce_value(value, kind or None, key)
    used = set(re.findall(r'\$([A-Za-z_][A-Za-z0-9_]*)', text))
    return {k: v for k, v in values.items() if k in used}
