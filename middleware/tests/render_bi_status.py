"""Render implementation/middleware/neo4j-bi-unrewritten-status.md from neug_fill_bi_status.py output.

FILL texts, queries, outputs and middleware details come from the status
JSON (actual run); the per-case analysis below is written by hand.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from cico import IndexMiddleware
from cico.middleware import strip_comments
from cico.neo4j import neo4j_profile
from cico.queryparts import split_parts

ROOT = Path(__file__).resolve().parents[1]

ORDER = ['bi11', 'bi4', 'bi8', 'bi16', 'bi19a', 'bi20', 'bi15', 'bi15w']
NEW = {'bi2', 'bi10', 'bi14', 'bi17'}

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
        '''- 可行路径：有限记录 CALL 按 `letter` 分支、`Tag.name` 唯一、日期按 UTC 规范化，只把 `cm` 换成 `person1.bi16MessageCountA/B`。
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
        '- 复现：`python3 middleware/tests/neug_fill_bi_status.py --output s.json && python3 middleware/tests/render_bi_status.py s.json`。', '',
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
              '- BI17：真实 Neo4j 上 20 个随机夹具 × delta∈{0,4,24}，60/60 一致（40 组非空，含零长度路径与同一棵回复树）。'
              '[结果](validation/neo4j-bi17-path-carrier-20261118.json)。BI17 不经 NeuG 绑定校验：NeuG 规划器对这个含 11 条关系的单个 MATCH 在 24 GB 上限下仍内存耗尽'
              '（README 7.7，上游问题），证明中记为 `neug_bind: skipped`，只做了转换和 schema 检查。', '',
              '## 声明约束', '',
              '用户 2026-11-18 同意。写在 `middleware/profiles/ldbc-snb-neo4j.yaml` 的 `constraints:`，是个别改写步骤的前提（不是定理本身的前提），'
              '用到的写入 `Result.premises`。`middleware/tests/constraint_checks.py` 在 SF1 实例（`bolt://127.0.0.1:7693`，只读）上 10/10 条 0 违反'
              '（[结果](validation/neo4j-constraint-checks-sf1-20261118.json)）。', '',
              '| 约束 | 已用于 | 计划用于 |', '| --- | --- | --- |',
              '| `(:Message)-[:REPLY_OF]->` 至多一条 | BI17 | BI4 |',
              '| `-[:CONTAINER_OF]->(:Post)` 至多一条 | BI17 | BI4 |',
              '| `(:Person)-[:IS_LOCATED_IN]->` 至多一条 | BI10（投影载体函数依赖） | BI11 |',
              '| `(:City)-[:IS_PART_OF]->` 至多一条 | BI10 | BI11 |',
              '| `(:Person)-[:IS_LOCATED_IN]->` 至少一条 | BI10（投影载体全覆盖：缺失分组不会读到默认值 `\'\'`） | BI11 |',
              '| `(:City)-[:IS_PART_OF]->` 至少一条 | BI10 | BI11 |',
              '| `(:Person)-[:IS_LOCATED_IN]->(:City)` | BI10（去掉冗余 City 类型谓词） | BI11 |',
              '| `(:City)-[:IS_PART_OF]->(:Country)` | BI10（去掉冗余 Country 类型谓词） | BI11 |',
              '| `Tag.name` 唯一 | | BI8（BI16） |',
              '| `Country.name` 唯一 | | BI11 |', '',
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
            lines += ['**原始 Neo4j 查询**', '', block(c['query_text'], 'cypher'),
                      '**中间件返回的 detail**', '', block(c['detail'] or '(无)'), '**卡点**', '', analysis, '']
            for number, source, internal in translate(mw, c['query_text'], parts):
                lines += [f'第 {number} 段原文与转换后的内部 Cypher（用于与 FILL 源对照；该段单独转换、未加输入桩，'
                          '实际探针中输入变量带标签，关系表会按端点裁剪）：', '',
                          block(source, 'cypher'), block(internal, 'cypher')]
            lines += ['**修复计划**', '', plan, '']
    lines += ['## 已改写的查询（本轮新增）', '']
    for name in ('bi2', 'bi10', 'bi14', 'bi17'):
        c = cases[name]
        lines += [f"### {name} → `{c['query']}`", '',
                  f"- 路径：`{c['route']}`；读取：{', '.join(f'`{i}`' for i in c['indexes'])}"
                  + (f"；参数特化：{', '.join('$' + p for p in c['specialized'])}" if c['specialized'] else ''),
                  f"- 前提（声明约束）：{', '.join(f'`{p}`' for p in c['premises']) or '无'}",
                  f"- 证明记录：{', '.join(sorted({p.get('rule', '') for p in c['proofs']})) or 'RewriterV4 + 回译验证'}", '',
                  '<details><summary>FILL、原始查询与改写输出</summary>', '']
        for fills in c['fills'].values():
            for fill in fills:
                lines.append(block(fill, 'cypher'))
        lines += ['原始查询：', '', block(c['query_text'], 'cypher'), '改写输出：', '', block(c['output'], 'cypher'),
                  '</details>', '']
    lines += ['## 本轮新增的中间件能力', '',
              '| 能力 | 文件 | 对应理论条件 |', '| --- | --- | --- |',
              '| 时间字面量规范化、定长 duration 折叠 | `cico/normalize.py` | $(M_F)$：同一瞬间同一字面量 |',
              '| 关系载体存在性（OPTIONAL + DISTINCT） | `cico/edge_carriers.py` | $(M_P)$ 图同构，$(M_F)$，$(L)$ 取 `count>0`；DISTINCT 另有语义论证 |',
              '| 声明约束与检查语句 | `cico/constraints.py`、`tests/constraint_checks.py` | 改写步骤的前提，`Result.premises` |',
              '| 变长关系唯一性的根路径证明 | `cico/neo4j.py`（`_root_path`、`_uniqueness_implied`、`contradiction`） | Neo4j 关系同构与内部同态等价的前提 |',
              '| 关系载体替换变长路径（回退阶段） | `cico/path_carriers.py` | $(L)$：至多一条路径 ⇒ $|G_k|=1$；同一子句的载体唯一性由过滤推出矛盾 |',
              '| 声明约束重试：类型谓词推导（含 `collect`/`UNWIND` 传递）、投影载体“恰好一个见证”检查 | `cico/neo4j.py`（`_implied_typed_labels`）、`cico/middleware.py`（`_relaxed`、`_projection_premises`） | $(M_F)$；投影载体 $|G_k|=1$ |', '']
    Path(out).write_text('\n'.join(lines))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('status')
    p.add_argument('--output', default=str(ROOT.parent / 'implementation/middleware/neo4j-bi-unrewritten-status.md'))
    a = p.parse_args()
    render(json.loads(Path(a.status).read_text()), a.output)
