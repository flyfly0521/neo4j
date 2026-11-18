"""Render implementation/middleware/neo4j-bi-unrewritten-status.md from neug_fill_bi_status.py output.

FILL texts, queries, outputs and middleware details come from the status
JSON (actual run); the per-case analysis below is written by hand.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from cico import IndexMiddleware
from cico.middleware import strip_comments
from cico.neo4j import neo4j_profile
from cico.queryparts import split_parts
from cico.neo4j import tokenize, render as render_tokens
from cico.syntax import closing

ROOT = Path(__file__).resolve().parents[1]

ORDER = ['bi16', 'bi19a', 'bi20', 'bi15', 'bi15w']
NEW = {'bi2', 'bi4', 'bi8', 'bi10', 'bi11', 'bi14', 'bi17'}

# case -> (one-line verdict, blocking analysis (markdown), fix plan (markdown), parts to translate (0-based))
ANALYSIS = {
    'bi10': (
        '可改写，下一步修复（声明约束重试阶段）',
        '''- 第 1 段（`MATCH (startPerson) CALL apoc.path.subgraphNodes ...`）是 APOC 过程，转换报 `CALL procedure: no internal equivalent`，按设计原样保留；第 2、3 段同理。
- 第 4 段（`UNWIND expertCandidatePersons ... RETURN`）可转换并绑定（APOC 输出类型由 profile 过程契约推出为 `PERSON[]`），但 RewriterV4 返回 `no_index_selected`。
- 不匹配的原因是 **$(M_F)$**：查询写 `(:City)`、`(:Country {name: $country})`，转换后是 `(:PLACE {type: 'City'})`、`(:PLACE {name: $country, type: 'Country'})`；FILL 源 `(country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)` 没有这两个类型谓词。查询过滤比索引过滤多出 `type` 条件，而这两个条件引用的是索引的隐藏顶点（City、Country），无法作为残余过滤 $h$ 补做（$(M_A)$(ii)）。''',
        '''- 声明约束 `(:Person)-[:IS_LOCATED_IN]->(:City)`、`(:City)-[:IS_PART_OF]->(:Country)` 使这两个类型谓词在每个匹配上恒真，去掉后查询过滤 = 索引过滤，$(M_F)$ 成立。
- `countryName` 是非聚合的投影载体，定理只覆盖可加聚合；需 `(:Person)-[:IS_LOCATED_IN]->` 至多一条、`(:City)-[:IS_PART_OF]->` 至多一条，保证每个 Person 至多一个见证（$|G_k|\\le 1$），存的值有定义，行重数不变。
- 实现：常规改写失败后的“声明约束重试”阶段（不改变 BI3 等已有输出形状，README 3.13）；重试结果读取投影载体时检查上述函数依赖，否则拒绝。代码已写好待部署（`p10`），见文末。''',
        [3]),
    'bi11': (
        '可改写，下一步修复：重试阶段已就绪，还需 `Country.name` 唯一处理节点身份',
        '''- 四段都能转换并绑定，均 `no_index_selected`（声明约束重试阶段也不匹配）。
- 类型谓词问题已由 BI10 的重试阶段解决；剩下的是：第 1 段把 `country` **节点**投影出去（`WITH DISTINCT country, a, b`），第 2、3 段用 `(b)-[:IS_LOCATED_IN]->(:City)-[:IS_PART_OF]->(country)` 与输入的同一个节点做身份连接。
- 索引 `PERSON(countryName)` 只公开 Person 和国家**名称**，Country 是隐藏顶点；第 2、3 段中 `country` 属于输入绑定（$H$），于是 $A\\cup(H\\cap V_I)\\subseteq I$ 不成立；第 1 段把隐藏顶点投影出去，$(K)$ 不成立。''',
        '''- BI10 的类型推导已部署。
- 再用声明约束 `Country.name` 唯一：路径终点是 Country 且名称等于 `country.name`，当且仅当它就是输入的 `country` 节点（需同时确认输入节点是 Country）。把身份连接改写为 `b.countryName = country.name`，Country 不再作为接口顶点，接口条件成立。
- 第 1 段：保留 `MATCH (country:Country {name: $country})`（唯一名称上的索引查找），把 `a` 的地点路径换成 `a.countryName = $country`。''',
        [0, 1, 2]),
    'bi4': (
        '可改写，需新的聚合分解规则',
        '''- 外层 `CALL { ... UNION ALL ... }`：两条分支在导入绑定下逐一编译，均 `no_index_selected`；去掉 CALL 的展平写法也不匹配。
- 不匹配的原因是 **聚合粒度**：查询第一分支按 Person 做 `count(DISTINCT message)`，消息来自 `topForums` 中的任意 `topForum1`，且作者必须是某个 `topForum2`（同样属于 `topForums`）的成员；索引 `BI4_MESSAGE_COUNT` 按 (Forum, Person) 分组存 `count(DISTINCT message)`，不公开 message，也不含会员约束。把按 Person 的 `count(DISTINCT)` 拆成按 (Forum, Person) 的计数之和，需要证明同一条消息不会出现在两个 Forum 的组里——RewriterV4 没有这类证明，$(L)$ 无法建立。''',
        '''- 新规则：消息的根帖唯一（`(:Message)-[:REPLY_OF]->` 至多一条 + 无环），帖子只属于一个论坛（`-[:CONTAINER_OF]->(:Post)` 至多一条），因此 message ↦ forum 是函数，不同 `topForum1` 的消息集合互不相交；`topForums` 来自 `WITH DISTINCT forum ... collect(...)`，元素互不相同。于是 $\\mathrm{count}_{\\mathrm{DISTINCT}}(\\text{按 Person}) = \\sum_{f\\in topForums} \\mathrm{messageCount}(f, person)$。
- 改写为 `UNWIND topForums AS topForum1 MATCH (topForum1)-[c:BI4_MESSAGE_COUNT]->(person) WHERE EXISTS { (person)<-[:HAS_MEMBER]-(f2) WHERE f2 IN topForums } RETURN person, sum(c.messageCount)`；UNION ALL 的零消息分支原样保留。''',
        []),
    'bi8': (
        '可改写，需 `Tag.name` 唯一 + 前端补充',
        '''- 第 2 段 `interestedPersons + collect(person)`（`NODE[] + NODE[]`）NeuG 不支持：`Binder exception: Cannot match a built-in function for given function +(NODE[],NODE[])`，第 3 段以后的前缀因此无法绑定（已有的列表拼接类型推断可给出类型桩）。
- 第 5、6 段的 `size([(tag)<-[:HAS_TAG]-(message:Message)-[:HAS_CREATOR]->(person) WHERE ... | message])` 交给相关标量计数规则，被拒绝：`scalar count lacks a fresh non-null graph witness` / `statement changed without reading an index`。
- 即使前端都通过，**接口条件**仍不成立：计数绑定外层**同一个 Tag 节点**（`MATCH (tag:Tag {name: $tag})` 得到的节点），索引 `PERSON(bi8MessageCount)` 按 Tag **名称**过滤、只公开 Person，Tag 是隐藏顶点却属于输入绑定 $H$。两个同名 Tag 时，索引存的是两者之和，原查询按单个节点计数。''',
        '''- 声明约束 `Tag.name` 唯一：外层 `tag` 由名称唯一确定，相关计数里的 `(tag)` 可换成新变量 `(t:Tag {name: $tag})`，Tag 不再是输入接口，$(M_P)$(ii) 成立，计数等于 `person.bi8MessageCount`。
- 前端：`size()` 区域的见证选择（`message` 是新鲜非空节点）需要修正；时间窗字面量问题已随 BI2 修复。''',
        [4]),
    'bi16': (
        '理论上可改写，收益低，暂缓',
        '''- `UNWIND [{letter:'A', tag:$tagA, date:$dateA}, {...B...}] AS param` 后进入相关 `CALL { WITH paramTagX, paramDateX ... }`；CALL 前缀的类型推断报 `no declared opaque type prefix`，CALL 分支未编译。
- CALL 内 `count(DISTINCT message1)`（`cm`）对应索引 A/B 列，但同一个 CALL 里还有 `OPTIONAL MATCH (person1)-[:KNOWS]-(person2)<-[:HAS_CREATOR]-(message2)-[:HAS_TAG]->(tag)`，再次使用第一个 MATCH 的**同一个 Tag 节点**并计算 `count(DISTINCT person2)`（`cp2`），索引不覆盖。
- `date(message1.creationDate) = date(paramDateX)` 与索引的 `[dateXStart, dateXEnd)` 区间之间的等价需要按 UTC 日边界证明。''',
        '''- 可行路径：有限记录 CALL 按 `letter` 分支；`Tag.name` 唯一且非空现已声明（BI8 的常量键绑定 T3 可复用，但 BI16 的 tag 来自 CALL 内的 MATCH，需要扩展 T3 的绑定来源）；`date(x) = date(p)` 按 UTC 日边界规范化；只把 `cm` 换成 `person1.bi16MessageCountA/B`。
- 判断：`cp2` 的好友 OPTIONAL 仍需现场计算，而改写后要按全体 Person 读属性筛选，可能比原查询从 Tag 出发更慢。暂缓，除非你需要。''',
        []),
    'bi19a': (
        '查询正文无可改写部分；需把 create-graph 脚本纳入范围',
        '''- `bi-19.cypher` 只在已存在的 GDS 图 `bi19` 上调用 `gds.shortestPath.dijkstra.stream`，转换报 `CALL procedure: no internal equivalent`；正文里没有权重计算，没有可匹配的片段。
- 权重在独立脚本 `bi-19-create-graph.cypher` 的 `gds.graph.project.cypher` **字符串参数**里：`count(replyOf)` over `(personA)<-[:HAS_CREATOR]-(:Message)-[replyOf:REPLY_OF]-(:Message)-[:HAS_CREATOR]->(personB)`，权重 `round(40-sqrt(n))`（至少 1）。''',
        '''- 若纳入范围：解析投影字符串，交互次数 = `knows.bi14_case1_fwd + knows.bi14_case1_rev`（无向 REPLY_OF 拆成两个方向），权重可直接读 `weight_bi19`。
- **必须加 `n > 0` 过滤**：原投影是 MATCH（只含有交互的人对），NeuG 的 `weight_bi19` 给无交互边 1e13；若直接投影全部 KNOWS 边会改变可达性。
- 需要你确认是否把 `bi-19-create-graph.cypher` 纳入改写范围。''',
        []),
    'bi20': (
        '查询正文无可改写部分；需把 create-graph 脚本纳入范围',
        '''- `bi-20.cypher` 同样只在已存在的 GDS 图 `bi20` 上调用 Dijkstra（`CALL procedure: no internal equivalent`）。
- 权重在 `bi-20-create-graph.cypher` 的投影字符串里：`min(abs(saA.classYear - saB.classYear)) + 1` over `(personA)-[:KNOWS]-(personB), (personA)-[saA:STUDY_AT]->(u:University)<-[saB:STUDY_AT]-(personB)`。''',
        '''- 若纳入范围：投影改读 `knows.bi20_direct`，并加 `bi20_direct <> 0` 保持原投影的边集（只含有共同大学的人对）。
- FILL 用 `(university:ORGANISATION)` 不区分 University/Company，需另声明约束 `(:Person)-[:STUDY_AT]->(:University)`。
- 需要你确认是否纳入范围。''',
        []),
    'bi15': (
        '不建议',
        '''- 权重计算写在 `gds.graph.project.cypher` 的字符串参数里，窗口日期经 `$startDate.epochMillis` 拼进字符串；前端在 CALL 处拒绝（`CALL procedure: no internal equivalent`），QueryPart 中看不到权重计算。
- 原投影的形状是 `OPTIONAL MATCH (pA)<-[:HAS_CREATOR]-(m1)-[r:REPLY_OF]-(m2)-[:HAS_CREATOR]->(pB)` + `OPTIONAL MATCH (m1)-[:REPLY_OF*0..]->(:Post)<-[:CONTAINER_OF]-(forum) WHERE 窗口` + `sum(CASE forum IS NOT NULL ... (m1:Post OR m2:Post) ...)`；FILL 是按方向拆开的两个计数 FILL 加一个合成 FILL（DAG），没有可复用的匹配。''',
        '''- 需要“GDS 投影字符串内嵌改写 + 专门的权重等价证明”（方向拆分、`label(target)='POST'` 与 `m1:Post OR m2:Post` 的对应、无交互默认值 1.0）。投入大，不建议。''',
        []),
    'bi15w': (
        '不建议（同 BI15）',
        '- 与 BI15 同一条原查询；定窗 FILL 只是把窗口条件放进了计数 FILL，卡点相同。',
        '- 同 BI15。',
        []),
}


def block(text, lang=''):
    text = text.strip('\n')
    fence = '```'
    while fence in text:
        fence += '`'
    return f'{fence}{lang}\n{text}\n{fence}\n'


def _attempt(mw, text, params):
    """(internal Cypher, note) through the middleware's own conversion
    (frontend + constant folding + NeuG language check), or (None, reason)."""
    try:
        return mw._to_internal(text), ''
    except Exception as first:  # noqa: BLE001 - the verdict is the content
        if params:
            try:
                return mw._to_internal(text, params), '（参数按第 0 组取值内联后转换）'
            except Exception:  # noqa: BLE001
                pass
        return None, str(first).strip()


def _call_branches(mw, text, params, where):
    from cico.subqueries import branches
    tokens, out, i = tokenize(text), [], 0
    while i < len(tokens):
        if tokens[i].kind == 'ident' and tokens[i].upper == 'CALL' and i + 1 < len(tokens) and tokens[i + 1].text == '{':
            close = closing(tokens, i + 1)
            for k, (lo, hi) in enumerate(branches(tokens[i + 2:close]), 1):
                body = render_tokens(tokens[i + 2 + lo:i + 2 + hi]).strip()
                internal, note = _attempt(mw, body, params)
                label = f'{where}CALL 第 {k} 个分支（单独转换，导入变量未加类型桩）'
                out.append((label + (note if internal is not None else ''),
                            internal if internal is not None else f'-- 转换失败：{note}'))
            i = close + 1
        else:
            i += 1
    return out


def neug_views(mw, text, params):
    """[(label, internal Cypher or failure)] for a Neo4j statement."""
    source = strip_comments(text)
    internal, note = _attempt(mw, source, params)
    if internal is not None:
        return [('整条语句' + note, internal)]
    views = [('整条语句', f'-- 转换失败：{note}')]
    parts = split_parts(source)
    if parts and len(parts) > 1:
        for i, part in enumerate(parts, 1):
            internal, note = _attempt(mw, part, params)
            views.append((f'第 {i} 段（单独转换，输入变量未加类型桩）' + (note if internal is not None else ''),
                          internal if internal is not None else f'-- 转换失败：{note}'))
            if internal is None:
                views += _call_branches(mw, part, params, f'第 {i} 段 ')
    else:
        views += _call_branches(mw, source, params, '')
    return views


def relaxed_views(mw, text, params):
    """The conversion the declared-constraint retry stage rewrites."""
    constants, _ = mw.frontend.key_constants(text)
    mw.frontend.relax = True
    try:
        return neug_views(mw, constants or text, params)
    finally:
        mw.frontend.relax = False


def view_lines(title, views):
    lines = [f'**{title}**', '']
    for label, text in views:
        lines += [f'{label}：', '', block(text, 'cypher')]
    return lines


def case_middleware(c):
    """A middleware with this case's NeuG files registered exactly as the status run did."""
    import re
    import workloads as W
    from neug_fill_bi_status import register_case
    mw = IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile())
    number = int(re.match(r'bi(\d+)', c['case']).group(1))
    register_case(mw, W.DOCS / 'NeuG/queries/index/ldbc-bi' / c['case'], number, case_parameters(c))
    return mw


def case_parameters(c):
    import workloads as W
    path = W.DOCS / 'Neo4j/queries/baseline/ldbc-bi' / c['query']
    return W.parameters('ldbc-bi', path, path.read_text())


def translate(mw, query, indexes):
    parts = split_parts(strip_comments(query)) or []
    out = []
    for i in indexes:
        if i >= len(parts):
            continue
        try:
            text = mw.frontend.to_internal(parts[i])
        except Exception as error:  # noqa: BLE001 - show the conversion verdict
            text = f'-- 转换失败: {error}'
        out.append((i + 1, parts[i], text))
    return out


def render(status, out):
    cases = {c['case']: c for c in status['cases']}
    done = [c for c in status['cases'] if c['rewritten']]
    lines = [
        '# Neo4j BI 原始查询：NeuG FILL 索引改写进度（2026-11-18）', '',
        '本文件由 `middleware/tests/render_bi_status.py` 从实测结果生成：FILL 文本、查询原文、中间件返回的原因和输出均取自 '
        '`middleware/tests/neug_fill_bi_status.py` 的运行结果（[JSON](validation/neo4j-bi-neug-fill-status-20261118.json)），'
        '卡点分析和修复计划为人工撰写。历史定位见 [BI 改写失败定位](neo4j-bi-neug-fill-rewrite-diagnosis.md)。', '',
        '## 口径', '',
        f"- 索引：实验文档 `NeuG/queries/index/ldbc-bi/<case>/`（提交 `{status['docs_commit'][:8]}`），NeuG DDL/FILL 直接注册；"
        '参数文件按 SF1 第 0 组参数渲染（BI13 的 `{}` 按 `run_workload.py` 用 `endDate` 渲染，BI15w 按文件注释的占位顺序渲染）。',
        '- 查询：`Neo4j/queries/baseline/ldbc-bi/bi-N.cypher` 原文；Neo4j profile 输入、输出。',
        f"- 中间件：提交 `{status['middleware_commit'][:8]}`（`develop`）。每个索引目录单独注册、单独改写一次；只编译，不物化索引、不执行 BI 查询。",
        '- 复现：`python3 middleware/tests/neug_fill_bi_status.py --output s.json && python3 middleware/tests/render_bi_status.py s.json`。',
        '- “方言转换后的 NeuG 查询”由中间件自身的转换路径生成（`_to_internal`：Neo4j 前端翻译 + 常量折叠 + NeuG 语言检查），与改写时交给 NeuG/RewriterV4 的文本一致。'
        '整条语句不能转换时（APOC/GDS 过程、`CALL {}`、列表拼接等），按 QueryPart 和 CALL 分支分别转换并给出原因；分段转换不带输入类型桩，'
        '实际改写的探针会为输入变量加标签。只有参数拼接（如 `duration({hours: $delta})`）阻止转换时，另给出按第 0 组参数内联后的结果。', '',
        f'## 总览：{len(done)}/{len(cases)} 改写（上一轮诊断 5/17）', '',
        '| 索引目录 | 查询 | 状态 | 读取的索引 | 路径 / 结论 |', '| --- | --- | --- | --- | --- |']
    for c in sorted(status['cases'], key=lambda c: (not c['rewritten'], int(''.join(ch for ch in c['case'] if ch.isdigit())))):
        if c['rewritten']:
            state = '✅ 本轮新增' if c['case'] in NEW else '✅ 原有'
            note = c['route'] or 'RewriterV4'
            if c['specialized']:
                note += f"；只对该组 {', '.join('$' + p for p in c['specialized'])} 有效"
        else:
            state, note = '❌', ANALYSIS[c['case']][0]
        lines.append(f"| {c['case']} | {c['query']} | {state} | {', '.join(f'`{i}`' for i in c['indexes']) or '—'} | {note} |")
    lines += ['', '本轮新增改写的验证：', '',
              '- BI2：6 个单测（等价瞬间同一字面量；其他窗口不改写；命名时区、月份单位不折叠）。',
              '- BI14：真实 Neo4j 上 30 个随机夹具 × 2 条查询，60/60 结果多重集一致（含双向、平行 KNOWS；无 KNOWS 时走 EXISTS 回退分支），事务全部回滚。'
              '[结果](validation/neo4j-bi14-carrier-existence-20261118.json)',
              '- BI10：SF1 实例上原查询与改写结果逐行一致 3/3 组参数（100/44/11 行；耗时 13.7s→0.09s、4.1s→0.07s、3.7s→0.07s，'
              '只读，`countryName` 已物化且 0 缺失、0 过期）。[结果](validation/neo4j-bi10-sf1-20261118.json)；'
              '另有单测：去掉任一类前提（类型域、至多一条、至少一条）都拒绝改写。',
              '- BI11：SF1 的参数样本计数全为 0（相等但不说明问题），另用随机夹具：20 个种子 × 2 个国家 × 2 个时间窗，80/80 一致，25 组计数非零。'
              '[夹具](validation/neo4j-bi11-fixture-20261118.json)、[SF1](validation/neo4j-bi11-sf1-20261118.json)',
              '- BI4：top forum 选择会扫描整个 SF1，测试把 CALL 之前的前缀换成夹具论坛列表（含一个重复元素），30/30 一致，145 行消息数非零；'
              '去掉改写中的 `WITH DISTINCT` 后同一检查失败（说明重复元素确实被测到）。[结果](validation/neo4j-bi4-fixture-20261118.json)',
              '- BI8：改写对 `$tag/$startDate/$endDate` 特化；夹具用唯一 Tag 名和固定时间窗注册 FILL，30/30 一致，169 行分数非零。'
              '[结果](validation/neo4j-bi8-fixture-20261118.json)',
              '- BI17：真实 Neo4j 上 20 个随机夹具 × delta∈{0,4,24}，60/60 一致（40 组非空，含零长度路径与同一棵回复树）。'
              '[结果](validation/neo4j-bi17-path-carrier-20261118.json)。BI17 不经 NeuG 绑定校验：NeuG 规划器对这个含 11 条关系的单个 MATCH 在 24 GB 上限下仍内存耗尽'
              '（README 7.7，上游问题），证明中记为 `neug_bind: skipped`，只做了转换和 schema 检查。', '',
              '## 声明约束', '',
              '用户 2026-11-18 同意。写在 `middleware/profiles/ldbc-snb-neo4j.yaml` 的 `constraints:`，是个别改写步骤的前提（不是定理本身的前提），'
              '用到的写入 `Result.premises`。`middleware/tests/constraint_checks.py` 在 SF1 实例（`bolt://127.0.0.1:7693`，只读）上 12/12 条 0 违反'
              '（[结果](validation/neo4j-constraint-checks-sf1-20261118.json)）。', '',
              '| 约束 | 已用于 | 计划用于 |', '| --- | --- | --- |',
              '| `(:Message)-[:REPLY_OF]->` 至多一条 | BI17、BI4（消息按论坛不相交） | |',
              '| `-[:CONTAINER_OF]->(:Post)` 至多一条 | BI17、BI4 | |',
              '| `(:Person)-[:IS_LOCATED_IN]->` 至多一条 | BI10、BI11（投影载体函数依赖） | |',
              '| `(:City)-[:IS_PART_OF]->` 至多一条 | BI10、BI11 | |',
              '| `(:Person)-[:IS_LOCATED_IN]->` 至少一条 | BI10、BI11（投影载体全覆盖：缺失分组不会读到默认值 `\'\'`） | |',
              '| `(:City)-[:IS_PART_OF]->` 至少一条 | BI10、BI11 | |',
              '| `(:Person)-[:IS_LOCATED_IN]->(:City)` | BI10、BI11（去掉冗余 City 类型谓词） | |',
              '| `(:City)-[:IS_PART_OF]->(:Country)` | BI10、BI11（去掉冗余 Country 类型谓词） | |',
              '| `Tag.name` 唯一且非空 | BI8（常量键绑定） | BI16 |',
              '| `Country.name` 唯一且非空 | BI11（键身份） | |', '',
              '## 未改写的查询（按计划修复顺序）', '']
    with IndexMiddleware(ROOT / 'schemas/ldbc-bi.yaml', profile=neo4j_profile()) as mw:
        for name in ORDER:
            c = cases[name]
            verdict, analysis, plan, parts = ANALYSIS[name]
            lines += [f"### {name} → `{c['query']}`：{verdict}", '',
                      f"- 中间件返回：`rewritten={c['rewritten']}`，`reason={c['reason']}`",
                      f"- 注册的索引：{', '.join(f'`{i}`' for i in c['indexes_registered'])}", '',
                      '**注册的 FILL（NeuG 方言，参数已渲染）**', '']
            for index_id, fills in c['fills'].items():
                for fill in fills:
                    lines.append(block(fill, 'cypher'))
            lines += ['**原始 Neo4j 查询**', '', block(c['query_text'], 'cypher')]
            with case_middleware(c) as cmw:
                lines += view_lines('方言转换后的 NeuG 查询', neug_views(cmw, c['query_text'], case_parameters(c)))
            lines += ['**中间件返回的 detail**', '', block(c['detail'] or '(无)'), '**卡点**', '', analysis, '']
            lines += ['**修复计划**', '', plan, '']
    lines += ['## 已改写的查询', '',
              '本轮新增在前，原有的在后。每条给出 FILL、原始查询、方言转换后的 NeuG 查询、改写输出，以及改写输出转换回 NeuG 方言的结果'
              '（关系载体存在性规则输出的 `startNode`、`EXISTS {}` 等 Neo4j 写法没有 NeuG 对应，会注明转换失败；这些规则的正确性见各自的 Neo4j 比对）。', '']
    rewritten = sorted((c for c in status['cases'] if c['rewritten']),
                       key=lambda c: (c['case'] not in NEW, int(''.join(ch for ch in c['case'] if ch.isdigit()))))
    for c in rewritten:
        name = c['case']
        lines += [f"### {name} → `{c['query']}`（{'本轮新增' if name in NEW else '原有'}）", '',
                  f"- 路径：`{c['route']}`；读取：{', '.join(f'`{i}`' for i in c['indexes'])}"
                  + (f"；参数特化：{', '.join('$' + p for p in c['specialized'])}" if c['specialized'] else ''),
                  f"- 前提（声明约束）：{', '.join(f'`{p}`' for p in c['premises']) or '无'}",
                  f"- 证明记录：{', '.join(sorted({p.get('rule', '') for p in c['proofs']})) or 'RewriterV4 + 回译验证'}", '',
                  '**注册的 FILL（NeuG 方言，参数已渲染）**', '']
        for fills in c['fills'].values():
            for fill in fills:
                lines.append(block(fill, 'cypher'))
        lines += ['**原始 Neo4j 查询**', '', block(c['query_text'], 'cypher')]
        params = case_parameters(c)
        with case_middleware(c) as cmw:
            lines += view_lines('方言转换后的 NeuG 查询', neug_views(cmw, c['query_text'], params))
            if c['route'].startswith('declared_constraints'):
                lines += view_lines('声明约束重试阶段实际改写的 NeuG 查询（类型谓词已由约束去掉、键身份已改写）',
                                    relaxed_views(cmw, c['query_text'], params))
            lines += ['**改写输出（Neo4j）**', '', block(c['output'], 'cypher')]
            lines += view_lines('改写输出转换回 NeuG 方言', neug_views(cmw, c['output'], params))
    lines += ['## 本轮新增的中间件能力', '',
              '| 能力 | 文件 | 对应理论条件 |', '| --- | --- | --- |',
              '| 时间字面量规范化、定长 duration 折叠 | `cico/normalize.py` | $(M_F)$：同一瞬间同一字面量 |',
              '| 关系载体存在性（OPTIONAL + DISTINCT） | `cico/edge_carriers.py` | $(M_P)$ 图同构，$(M_F)$，$(L)$ 取 `count>0`；DISTINCT 另有语义论证 |',
              '| 声明约束与检查语句 | `cico/constraints.py`、`tests/constraint_checks.py` | 改写步骤的前提，`Result.premises` |',
              '| 变长关系唯一性的根路径证明 | `cico/neo4j.py`（`_root_path`、`_uniqueness_implied`、`contradiction`） | Neo4j 关系同构与内部同态等价的前提 |',
              '| 关系载体替换变长路径（回退阶段） | `cico/path_carriers.py` | $(L)$：至多一条路径 ⇒ $|G_k|=1$；同一子句的载体唯一性由过滤推出矛盾 |',
              '| 声明约束重试：类型谓词推导（含 `collect`/`UNWIND` 传递）、投影载体“恰好一个见证”检查 | `cico/neo4j.py`（`_implied_typed_labels`）、`cico/middleware.py`（`_relaxed`、`_projection_premises`） | $(M_F)$；投影载体 $|G_k|=1$ |',
              '| 键身份（T1 延后绑定、T2 键比较） | `cico/neo4j.py`（`_key_identities`） | $(M_P)$(ii)：链终点成为隐藏顶点 |',
              '| DISTINCT 计数按键列表分区（CALL 分支回退） | `cico/distinct_partitions.py` | $(M_P)$ 子图嵌入、接口公开、$(M_A)$(ii)；分组不相交由声明约束证明 |',
              '| 常量键绑定（T3）与计数见证选择 | `cico/neo4j.py`（`key_constants`）、`cico/scalar_subqueries.py` | $(M_P)$(ii)：计数区域不再以 Tag 为输入接口 |', '']
    Path(out).write_text('\n'.join(lines))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('status')
    p.add_argument('--output', default=str(ROOT.parent / 'implementation/middleware/neo4j-bi-unrewritten-status.md'))
    a = p.parse_args()
    render(json.loads(Path(a.status).read_text()), a.output)
