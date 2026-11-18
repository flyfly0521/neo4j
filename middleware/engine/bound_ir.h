// Bound IR v2: a complete, printer-oriented serialization of a NeuG bound
// statement.  Unlike structuredBoundStatementJson (v1, a proof-oriented
// snapshot), v2 serializes every construct the middleware may print:
// UNWIND, variable-length relationships with bounds, nested literals,
// subqueries, variables and function result types.  Anything else is
// emitted with "unsupported": true so the printer can fall back safely.
#pragma once

#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

#include <set>
#include <string>

#include "neug/compiler/binder/bound_statement.h"
#include "neug/compiler/binder/expression/aggregate_function_expression.h"
#include "neug/compiler/binder/expression/case_expression.h"
#include "neug/compiler/binder/expression/expression.h"
#include "neug/compiler/binder/expression/lambda_expression.h"
#include "neug/compiler/binder/expression/literal_expression.h"
#include "neug/compiler/binder/expression/node_expression.h"
#include "neug/compiler/binder/expression/parameter_expression.h"
#include "neug/compiler/binder/expression/property_expression.h"
#include "neug/compiler/binder/expression/rel_expression.h"
#include "neug/compiler/binder/expression/scalar_function_expression.h"
#include "neug/compiler/binder/expression/subquery_expression.h"
#include "neug/compiler/binder/expression/variable_expression.h"
#include "neug/compiler/binder/query/bound_regular_query.h"
#include "neug/compiler/binder/query/normalized_query_part.h"
#include "neug/compiler/binder/query/normalized_single_query.h"
#include "neug/compiler/binder/query/query_graph.h"
#include "neug/compiler/binder/query/reading_clause/bound_match_clause.h"
#include "neug/compiler/binder/query/reading_clause/bound_unwind_clause.h"
#include "neug/compiler/binder/query/return_with_clause/bound_projection_body.h"
#include "neug/compiler/catalog/catalog_entry/table_catalog_entry.h"
#include "neug/compiler/common/enums/expression_type.h"
#include "neug/compiler/common/types/types.h"
#include "neug/compiler/common/types/value/nested.h"
#include "neug/compiler/common/types/value/value.h"
#include "neug/compiler/function/gds/rec_joins.h"

namespace cico {

class BoundIR {
 public:
  using V = rapidjson::Value;
  explicit BoundIR(rapidjson::Document::AllocatorType& allocator) : a(allocator) {}

  V statement(const neug::binder::BoundStatement& s) {
    V out(rapidjson::kObjectType);
    put(out, "version", V(2));
    put(out, "statement_type", V(uint64_t(s.getStatementType())));
    if (s.getStatementType() != ::neug::common::StatementType::QUERY) {
      put(out, "unsupported", V(true));
      return out;
    }
    const auto& q = s.constCast<::neug::binder::BoundRegularQuery>();
    V singles(rapidjson::kArrayType), unions(rapidjson::kArrayType), pre(rapidjson::kArrayType);
    for (uint32_t i = 0; i < q.getNumSingleQueries(); ++i) {
      singles.PushBack(single(*q.getSingleQuery(i)), a);
      if (i > 0) unions.PushBack(V(q.getIsUnionAll(i - 1)), a);
    }
    for (const auto& p : q.getPreQueryPart()) pre.PushBack(part(p), a);
    put(out, "single_queries", std::move(singles));
    put(out, "union_all", std::move(unions));
    put(out, "pre_parts", std::move(pre));
    put(out, "pre_expressions", exprs(q.getPreQueryExprs()));
    put(out, "post_query", V(q.getPostSingleQuery() != nullptr));
    return out;
  }

 private:
  rapidjson::Document::AllocatorType& a;
  std::set<const neug::binder::Expression*> path;

  V str(const std::string& s) { return V(s.data(), s.size(), a); }
  void put(V& o, const char* k, V v) { o.AddMember(V(k, a), v, a); }
  void put(V& o, const char* k, const std::string& v) { put(o, k, str(v)); }

  V single(const neug::binder::NormalizedSingleQuery& q) {
    V parts(rapidjson::kArrayType);
    for (uint32_t i = 0; i < q.getNumQueryParts(); ++i) parts.PushBack(part(*q.getQueryPart(i)), a);
    V o(rapidjson::kObjectType);
    put(o, "parts", std::move(parts));
    return o;
  }

  V part(const neug::binder::NormalizedQueryPart& p) {
    V o(rapidjson::kObjectType), reading(rapidjson::kArrayType), updating(rapidjson::kArrayType);
    for (uint32_t i = 0; i < p.getNumReadingClause(); ++i) reading.PushBack(clause(*p.getReadingClause(i)), a);
    for (uint32_t i = 0; i < p.getNumUpdatingClause(); ++i) {
      V u(rapidjson::kObjectType);
      put(u, "clause_type", V(uint64_t(p.getUpdatingClause(i)->getClauseType())));
      put(u, "unsupported", V(true));
      updating.PushBack(u, a);
    }
    put(o, "reading", std::move(reading));
    put(o, "updating", std::move(updating));
    put(o, "projection", p.hasProjectionBody() ? projection(*p.getProjectionBody()) : V());
    put(o, "predicate", expr(p.getProjectionBodyPredicate().get()));
    return o;
  }

  V projection(const neug::binder::BoundProjectionBody& b) {
    V o(rapidjson::kObjectType), names(rapidjson::kArrayType), orders(rapidjson::kArrayType);
    put(o, "distinct", V(b.isDistinct()));
    put(o, "expressions", exprs(b.getProjectionExpressions()));
    for (const auto& n : b.getProjectionNames()) names.PushBack(str(n), a);
    put(o, "names", std::move(names));
    put(o, "group_by", exprs(b.getGroupByExpressions()));
    put(o, "aggregates", exprs(b.getAggregateExpressions()));
    put(o, "order_by", exprs(b.getOrderByExpressions()));
    for (bool asc : b.getSortingOrders()) orders.PushBack(V(asc), a);
    put(o, "ascending", std::move(orders));
    put(o, "skip", expr(b.getSkipNumber().get()));
    put(o, "limit", expr(b.getLimitNumber().get()));
    return o;
  }

  V clause(const neug::binder::BoundReadingClause& c) {
    V o(rapidjson::kObjectType);
    put(o, "clause_type", V(uint64_t(c.getClauseType())));
    put(o, "predicate", expr(c.getPredicate().get()));
    if (c.getClauseType() == ::neug::common::ClauseType::MATCH) {
      const auto& m = c.constCast<::neug::binder::BoundMatchClause>();
      put(o, "kind", "MATCH");
      put(o, "optional", V(m.getMatchClauseType() == ::neug::common::MatchClauseType::OPTIONAL_MATCH));
      put(o, "has_hint", V(m.getHint() != nullptr));
      put(o, "graphs", graphs(*m.getQueryGraphCollection()));
    } else if (c.getClauseType() == ::neug::common::ClauseType::UNWIND) {
      const auto& u = c.constCast<::neug::binder::BoundUnwindClause>();
      put(o, "kind", "UNWIND");
      put(o, "in", expr(u.getInExpr().get()));
      put(o, "out", expr(u.getOutExpr().get()));
    } else {
      put(o, "kind", "UNSUPPORTED");
      put(o, "unsupported", V(true));
    }
    return o;
  }

  V graphs(const neug::binder::QueryGraphCollection& collection) {
    V list(rapidjson::kArrayType);
    for (neug::common::idx_t g = 0; g < collection.getNumQueryGraphs(); ++g) {
      const auto* graph = collection.getQueryGraph(g);
      V item(rapidjson::kObjectType), nodes(rapidjson::kArrayType), rels(rapidjson::kArrayType);
      for (const auto& node : graph->getQueryNodes()) nodes.PushBack(entity(*node), a);
      for (const auto& rel : graph->getQueryRels()) rels.PushBack(entity(*rel), a);
      put(item, "nodes", std::move(nodes));
      put(item, "rels", std::move(rels));
      list.PushBack(item, a);
    }
    return list;
  }

  V entity(const neug::binder::NodeOrRelExpression& e) {
    V o(rapidjson::kObjectType), tables(rapidjson::kArrayType);
    put(o, "uid", e.getUniqueName());
    put(o, "raw", e.getVariableName());
    for (auto* entry : e.getEntries()) tables.PushBack(str(entry->getName()), a);
    put(o, "tables", std::move(tables));
    const auto* rel = dynamic_cast<const ::neug::binder::RelExpression*>(&e);
    if (!rel) {
      put(o, "kind", "NODE");
      return o;
    }
    put(o, "kind", "REL");
    put(o, "src", rel->getSrcNodeName());
    put(o, "dst", rel->getDstNodeName());
    put(o, "direction", rel->getDirectionType() == ::neug::binder::RelDirectionType::SINGLE ? "SINGLE"
                        : rel->getDirectionType() == ::neug::binder::RelDirectionType::BOTH ? "BOTH" : "UNKNOWN");
    put(o, "rel_type", V(uint64_t(rel->getRelType())));
    if (rel->isRecursive()) {
      V r(rapidjson::kObjectType);
      const auto* info = rel->getRecursiveInfo();
      if (info == nullptr || info->bindData == nullptr) {
        put(r, "unsupported", V(true));
      } else {
        put(r, "lower", V(uint64_t(info->bindData->lowerBound)));
        put(r, "upper", V(uint64_t(info->bindData->upperBound)));
        put(r, "semantic", V(uint64_t(info->bindData->semantic)));
        put(r, "node_predicate", expr(info->nodePredicate.get()));
        put(r, "rel_predicate", expr(info->relPredicate.get()));
        put(r, "weighted", V(info->bindData->weightPropertyExpr != nullptr));
      }
      put(o, "recursive", std::move(r));
    }
    return o;
  }

  V exprs(const neug::binder::expression_vector& list) {
    V out(rapidjson::kArrayType);
    for (const auto& e : list) out.PushBack(expr(e.get()), a);
    return out;
  }

  V value(const neug::common::Value& v) {
    V o(rapidjson::kObjectType);
    const auto& type = v.getDataType();
    put(o, "type", type.toString());
    put(o, "null", V(v.isNull()));
    if (v.isNull()) return o;
    const auto id = type.getLogicalTypeID();
    if (id == ::neug::common::LogicalTypeID::LIST || id == ::neug::common::LogicalTypeID::ARRAY ||
        id == ::neug::common::LogicalTypeID::STRUCT) {
      V items(rapidjson::kArrayType);
      for (uint32_t i = 0; i < ::neug::common::NestedVal::getChildrenSize(&v); ++i)
        items.PushBack(value(*::neug::common::NestedVal::getChildVal(&v, i)), a);
      put(o, "items", std::move(items));
      if (id == ::neug::common::LogicalTypeID::STRUCT) {
        V names(rapidjson::kArrayType);
        for (const auto& n : ::neug::common::StructType::getFieldNames(type)) names.PushBack(str(n), a);
        put(o, "fields", std::move(names));
      }
      return o;
    }
    put(o, "text", v.toString());
    return o;
  }

  V expr(const neug::binder::Expression* e) {
    using namespace ::neug::binder;
    if (e == nullptr) return V();
    V o(rapidjson::kObjectType);
    put(o, "k", ::neug::common::ExpressionTypeUtil::toString(e->expressionType));
    put(o, "uid", e->getUniqueName());
    put(o, "type", e->getDataType().toString());
    put(o, "alias", e->getAlias());
    if (path.contains(e) || path.size() > 256) {
      put(o, "unsupported", V(true));
      put(o, "reason", "cycle_or_depth");
      return o;
    }
    path.insert(e);
    bool known = true;
    if (auto* p = dynamic_cast<const PropertyExpression*>(e)) {
      put(o, "owner", p->getVariableName());
      put(o, "owner_raw", p->getRawVariableName());
      put(o, "property", p->getPropertyName());
    } else if (auto* l = dynamic_cast<const LiteralExpression*>(e)) {
      put(o, "value", value(l->getValue()));
    } else if (auto* p = dynamic_cast<const ParameterExpression*>(e)) {
      put(o, "name", p->getName());
    } else if (auto* v = dynamic_cast<const VariableExpression*>(e)) {
      put(o, "name", v->getVariableName());
    } else if (auto* n = dynamic_cast<const NodeOrRelExpression*>(e)) {
      put(o, "entity", n->getUniqueName());
      put(o, "raw", n->getVariableName());
      put(o, "entity_kind", dynamic_cast<const RelExpression*>(n) ? "REL" : "NODE");
    } else if (auto* f = dynamic_cast<const AggregateFunctionExpression*>(e)) {
      put(o, "function", f->getFunction().name);
      put(o, "distinct", V(f->isDistinct()));
    } else if (auto* f = dynamic_cast<const ScalarFunctionExpression*>(e)) {
      put(o, "function", f->getFunction().name);
    } else if (auto* c = dynamic_cast<const CaseExpression*>(e)) {
      V alternatives(rapidjson::kArrayType);
      for (uint64_t i = 0; i < c->getNumCaseAlternatives(); ++i) {
        V alt(rapidjson::kObjectType);
        put(alt, "when", expr(c->getCaseAlternative(i)->whenExpression.get()));
        put(alt, "then", expr(c->getCaseAlternative(i)->thenExpression.get()));
        alternatives.PushBack(alt, a);
      }
      put(o, "alternatives", std::move(alternatives));
      put(o, "else", expr(c->getElseExpression().get()));
    } else if (auto* s = dynamic_cast<const SubqueryExpression*>(e)) {
      const auto type = s->getSubqueryType();
      put(o, "subquery", type == ::neug::common::SubqueryType::EXISTS ? "EXISTS"
                         : type == ::neug::common::SubqueryType::COUNT ? "COUNT" : "UNKNOWN");
      put(o, "has_hint", V(s->getHint() != nullptr));
      if (s->getQueryGraphCollection()) put(o, "graphs", graphs(*s->getQueryGraphCollection()));
      else known = false;
      put(o, "where", expr(s->getWhereExpression().get()));
    } else {
      known = false;  // PATH, LAMBDA, STAR, GRAPH, ...
    }
    if (!known) put(o, "unsupported", V(true));
    put(o, "children", exprs(e->getChildren()));
    path.erase(e);
    return o;
  }
};

inline std::string dumpJson(const rapidjson::Value& v) {
  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  v.Accept(writer);
  return {buffer.GetString(), buffer.GetSize()};
}

}  // namespace cico
