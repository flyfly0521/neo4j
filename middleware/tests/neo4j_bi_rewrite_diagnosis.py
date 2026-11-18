"""Separate Neo4j frontend gaps from NeuG index matching gaps (compile only).

Uses manifest sample 0 for BI2/8/10/16. Counterfactual FILL definitions are
diagnostic controls, not production replacements: removing OPTIONAL changes
the domain of zero-valued groups. Never opens or executes a graph database.
"""
import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from cico import IndexMiddleware
from cico.engine import resolve_binary
from cico.middleware import inline_parameters
from cico.statements import parse_fill
import workloads as W

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'middleware/schemas/ldbc-bi.yaml'


def file_info(path):
    path = Path(path)
    return dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def git(*args):
    return subprocess.check_output(['git', '-C', str(W.DOCS), *args], text=True).strip()


def fold_days(text):
    """Only the fixed, timestamp-without-zone BI2 control, not a general rule."""
    def replace(match):
        start = datetime.datetime.fromisoformat(match[1])
        end = start + datetime.timedelta(days=int(match[2]))
        return "TIMESTAMP('" + end.isoformat(sep=' ') + "')"
    return re.sub(r"TIMESTAMP\('([^']+)'\)\s*\+\s*duration\('(\d+) days'\)",
                  replace, text, flags=re.I)


def probe(mw, query):
    row = dict(query=query)
    try:
        mw.engine.bind(query)
        row['baseline_bound'] = True
        raw = mw.engine.rewrite(query)
        row.update(used=mw._carriers_read(raw.get('ir', {})), changed=raw.get('changed'),
                   cost_compared=raw.get('cost_compared'), index_entries=raw.get('index_entries'))
    except Exception as error:
        row['error'] = str(error)
    return row


def registered_probe(factory, fills, queries):
    with factory('neug') as mw:
        registrations = [mw.execute(fill).to_dict() for fill in fills]
        return dict(fills=fills, registration=registrations, catalog=mw.engine.indexes(),
                    probes={name: probe(mw, query) for name, query in queries.items()})


def run(engine=None):
    def factory(profile):
        kwargs = {'engine_binary': engine} if engine else {}
        return IndexMiddleware(SCHEMA, profile=profile, **kwargs)

    report = dict(docs_path=str(W.DOCS), docs_commit=git('rev-parse', 'HEAD'),
                  docs_branch=git('branch', '--show-current'), sample=0,
                  workload_queries_executed=False, physical_indexes_built=False,
                  controls_are_production_fixes=False, sources=[], stages=[],
                  source_controls=[], scalar_controls=[], group_controls=[], docs_controls=[])
    report['sources'].append(file_info(SCHEMA))
    binary = engine or resolve_binary('incgq_rewrite_engine', 'CICO_ENGINE')
    report['engine'] = file_info(binary)
    library = ROOT.parent / 'IncGQ/build/src/libneug.so'
    if library.is_file():
        report['library'] = file_info(library)

    for number in (2, 8, 10, 16):
        query_path = W.DOCS / f'Neo4j/queries/baseline/ldbc-bi/bi-{number}.cypher'
        query = query_path.read_text()
        params = W.parameters('ldbc-bi', query_path, query)
        index_path = W.DOCS / f'Neo4j/queries/index/ldbc-bi/bi{number}' / (
            'index.cypher' if number == 10 else 'parameter.cypher')
        report['sources'].extend([file_info(query_path), file_info(index_path)])
        with factory('neo4j') as mw:
            registration = [r.to_dict() for r in mw.register_neo4j_index(
                index_path.read_text(), parameters=params)]
            fills = [f for group in mw.fills.values() for f in group]
            row = dict(number=number, parameters=params, registration=registration, fills=fills,
                       exact_sources=[probe(mw, parse_fill(f).source) for f in fills],
                       whole_query=mw.execute(query, parameters=params).to_dict())
            try:
                row['translated_query'] = mw._to_internal(query, params)
            except Exception as error:
                row['translation_error'] = str(error)
            if number == 10:
                fragment = ('MATCH (person:Person)-[:IS_LOCATED_IN]->(:City)'
                            '-[:IS_PART_OF]->(country:Country) WHERE country.name=$country '
                            'RETURN person.id AS personId,country.name AS countryName')
                row['neo4j_fragment'] = mw.execute(
                    fragment, parameters={'country': params['country']}).to_dict()
            report['stages'].append(row)

        if number == 10:
            continue
        for optional in (True, False):
            for fold in (False, True):
                control = list(fills)
                if not optional:
                    control = [re.sub(r'MATCH \(\w+:\w+\) OPTIONAL MATCH', 'MATCH',
                                      f, count=1) for f in control]
                if fold:
                    control = [fold_days(f) for f in control]
                row = registered_probe(factory, control, {
                    f'source_{i}': parse_fill(f).source for i, f in enumerate(control)})
                row.update(number=number, keep_optional=optional, fold_timestamp_days=fold)
                report['source_controls'].append(row)

        # The docs' hand-written NeuG queries are independent controls, not
        # automatic translations. Their comments assume TAG.name uniqueness.
        neu_params = dict(params)
        if number == 2:
            neu_params.update(windowStart=params['date'],
                              windowMid=params['date'] + datetime.timedelta(days=100),
                              windowEnd=params['date'] + datetime.timedelta(days=200))
        if number == 16:
            for suffix in ('A', 'B'):
                start = params[f'date{suffix}'].replace(hour=0, minute=0, second=0, microsecond=0)
                neu_params.update({f'date{suffix}Start': start,
                                   f'date{suffix}End': start + datetime.timedelta(days=1)})
        neu_index = W.DOCS / f'NeuG/queries/index/ldbc-bi/bi{number}/parameter.cypher'
        neu_query = W.DOCS / f'NeuG/queries/baseline/ldbc-bi/bi{number}/query.cypher'
        report['sources'].extend([file_info(neu_index), file_info(neu_query)])
        neu_text = inline_parameters(neu_query.read_text(), neu_params)
        for variant in ('converted_index', 'docs_neug_index'):
            with factory('neug' if variant == 'docs_neug_index' else 'neo4j') as mw:
                if variant == 'docs_neug_index':
                    reg = mw.execute_script(inline_parameters(neu_index.read_text(), neu_params))
                else:
                    reg = mw.register_neo4j_index(index_path.read_text(), parameters=params)
                report['docs_controls'].append(dict(
                    number=number, variant=variant, registration=[r.to_dict() for r in reg],
                    catalog=mw.engine.indexes(), probe=probe(mw, neu_text)))

    # Same registered source/query, only vary scalar predicates and OPTIONAL.
    for optional in (False, True):
        for name, predicate in (
            ('none', ''),
            ('timestamp_range', "m.creationDate>=TIMESTAMP('2010-06-09 00:00:00') AND "
                                "m.creationDate<TIMESTAMP('2010-09-17 00:00:00')"),
            ('timestamp_add', "m.creationDate>=TIMESTAMP('2010-06-09 00:00:00') AND "
                              "m.creationDate<TIMESTAMP('2010-06-09 00:00:00')+duration('100 days')")):
            query = ('MATCH(t:TAG) ' + ('OPTIONAL MATCH' if optional else 'MATCH') +
                     '(m:POST:COMMENT)-[:HASTAG]->(t)' +
                     (' WHERE ' + predicate if predicate else '') + ' RETURN t.id,count(m)')
            row = registered_probe(factory, ['FILL TAG(probeCount) FROM (' + query + ')'],
                                   {'exact': query})
            row.update(optional=optional, predicate=name)
            report['scalar_controls'].append(row)

    for distinct in (False, True):
        for name, predicate in (
            ('none', ''),
            ('date', "date(m.creationDate)=date(TIMESTAMP('2012-07-26 00:00:00'))"),
            ('tag_range', "t.name='Marc_Gicquel' AND "
                          "m.creationDate>=TIMESTAMP('2012-07-26 00:00:00') AND "
                          "m.creationDate<TIMESTAMP('2012-07-27 00:00:00')")):
            pattern = ('MATCH(p:PERSON)<-[:HASCREATOR]-(m:POST:COMMENT)-[:HASTAG]->(t:TAG)' +
                       (' WHERE ' + predicate if predicate else ''))
            aggregate = 'count(' + ('DISTINCT ' if distinct else '') + 'm) AS cm'
            source = pattern + ' RETURN p.id,' + aggregate
            queries = dict(exact=source,
                           with_only=pattern + ' WITH p,' + aggregate + ' RETURN p.id,cm',
                           having=pattern + ' WITH p,' + aggregate + ' WHERE cm>0 RETURN p.id,cm',
                           optional=pattern.replace('MATCH(p:PERSON)',
                                                    'MATCH(p:PERSON) OPTIONAL MATCH(p)', 1) +
                                    ' RETURN p.id,' + aggregate)
            row = registered_probe(factory, ['FILL PERSON(probeCount) FROM (' + source + ')'], queries)
            row.update(distinct=distinct, predicate=name)
            report['group_controls'].append(row)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engine', type=Path, help='Optional diagnostic engine; default is middleware selection')
    args = parser.parse_args()
    result = run(args.engine)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n')
    for stage in result['stages']:
        print('BI' + str(stage['number']), 'source reads:', [p.get('used') for p in stage['exact_sources']],
              'whole query:', stage['whole_query']['reason'])
    print('Evidence:', args.output)
