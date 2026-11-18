// IncGQ middleware engine (compile-only JSON-lines service).
//
// Never opens a database and never executes DDL, FILL or queries.  It owns two
// in-memory compiler catalogs built from the same schema YAML:
//   indexed  - FILL definitions are bound and marked READY (simulated)
//   baseline - schema only; used to bind printed Cypher for verification and
//              to describe FILL source queries
// The schema can be replaced at runtime (`load`), after which every registered
// FILL candidate is registered again.  Candidates can be added (`register`)
// and removed (`drop`) at any time.
//
// `rewrite` runs the production ClientContext::prepare (RewriterV4 + cost
// comparison), then reproduces the *selected* bound statement by re-binding the
// original query along candidate replay routes until its v1 structured JSON
// equals NeuG's own trace of the selected route exactly.  That statement is
// returned as Bound IR v2 (bound_ir.h).
//
// Protocol: one JSON request per line on stdin, one JSON response per line on
// the original stdout; everything else NeuG prints goes to stderr.

#include <rapidjson/document.h>
#include <unistd.h>
#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cstdio>
#include <deque>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "bound_ir.h"
#include "neug/compiler/binder/binder.h"
#include "neug/compiler/binder/copy/bound_copy_from.h"
#include "neug/compiler/binder/rewriter/bound_rewrite_trace.h"
#include "neug/compiler/catalog/catalog.h"
#include "neug/compiler/catalog/catalog_entry/fill_index_aux_info.h"
#include "neug/compiler/catalog/catalog_entry/rel_table_catalog_entry.h"
#include "neug/compiler/gopt/g_alias_manager.h"
#include "neug/compiler/main/client_context.h"
#include "neug/compiler/main/metadata_manager.h"
#include "neug/compiler/main/prepared_statement.h"
#include "neug/compiler/parser/copy.h"
#include "neug/compiler/parser/parser.h"
#include "neug/compiler/planner/operator/logical_plan.h"

namespace {
using J = rapidjson::Value;
using Doc = rapidjson::Document;
using Context = neug::main::ClientContext;
using Metadata = neug::main::MetadataManager;

constexpr uint64_t PROTOCOL_VERSION = 2;
constexpr size_t MAX_REPLAY_BINDS = 512;

std::string readFile(const std::string& path) {
  std::ifstream file(path);
  if (!file) throw std::invalid_argument("Cannot read " + path);
  std::ostringstream result;
  result << file.rdbuf();
  return result.str();
}

const J& member(const J& object, const char* name) {
  static const J null;
  if (!object.IsObject()) throw std::invalid_argument("Expected a JSON object");
  auto it = object.FindMember(name);
  return it == object.MemberEnd() ? null : it->value;
}

std::string text(const J& value, const char* what) {
  if (!value.IsString()) throw std::invalid_argument(std::string(what) + " must be a string");
  return {value.GetString(), value.GetStringLength()};
}

struct Out {
  Doc doc;
  Out() { doc.SetObject(); }
  Doc::AllocatorType& a() { return doc.GetAllocator(); }
  J str(const std::string& s) { return J(s.data(), s.size(), a()); }
  void put(J& o, const char* k, J v) { o.AddMember(str(k), v, a()); }
  void put(const char* k, J v) { put(doc, k, std::move(v)); }
  void put(const char* k, const std::string& v) { put(k, str(v)); }
  J strings(const std::vector<std::string>& values) {
    J r(rapidjson::kArrayType);
    for (const auto& v : values) r.PushBack(str(v), a());
    return r;
  }
  J parsed(const std::string& json) {
    Doc d;
    d.Parse(json.data(), json.size());
    if (d.HasParseError()) throw std::runtime_error("Invalid JSON produced by NeuG serializer");
    return J(d, a());
  }
  std::string dump() const { return cico::dumpJson(doc); }
};

struct Candidate {
  std::string id;
  std::vector<std::string> fills;
};

struct IndexRecord {
  std::string candidate, name, table, fillType, sourceQuery;
  std::vector<std::string> columns;
};

struct Failure : std::runtime_error {
  std::string stage;
  Failure(std::string stage, const std::string& message)
      : std::runtime_error(message), stage(std::move(stage)) {}
};

// OPTIONAL sources retain broad relationship-group entries at bind time.
// Match the query-side label analyzer without mutating the bound source (an
// OPTIONAL must never narrow the domain of its already-bound outer nodes).
// Only impossible physical partitions of fixed-length edges are removed.
void normalizeFillPartitions(neug::catalog::FillIndexAuxInfo& info,
                             neug::catalog::Catalog& catalog,
                             const neug::transaction::Transaction* transaction) {
  auto& pattern = info.getPatternIR();
  for (auto& rel : pattern.relsInfo) {
    if (rel.relType != neug::common::QueryRelType::NON_RECURSIVE) continue;
    const auto& src = pattern.nodesInfo.at(rel.srcNodeIdx).nodeLabels;
    const auto& dst = pattern.nodesInfo.at(rel.dstNodeIdx).nodeLabels;
    const auto contains = [](const auto& labels, auto id) {
      return std::find(labels.begin(), labels.end(), id) != labels.end();
    };
    std::erase_if(rel.relLabels, [&](auto id) {
      const auto& table = catalog.getTableCatalogEntry(transaction, id)
                              ->template constCast<neug::catalog::RelTableCatalogEntry>();
      const bool forward = contains(src, table.getSrcTableID()) && contains(dst, table.getDstTableID());
      const bool reverse = rel.edgeType == neug::common::EdgeType::UNDIRECTED &&
          contains(dst, table.getSrcTableID()) && contains(src, table.getDstTableID());
      return !forward && !reverse;
    });
  }
  // Pattern matching uses both the edge vector and adjacency lists.
  for (auto& node : pattern.nodesInfo) {
    node.outgoing.clear(); node.incoming.clear(); node.undirected.clear();
  }
  for (const auto& rel : pattern.relsInfo) {
    auto& src = pattern.nodesInfo.at(rel.srcNodeIdx);
    auto& dst = pattern.nodesInfo.at(rel.dstNodeIdx);
    if (rel.edgeType == neug::common::EdgeType::UNDIRECTED) {
      src.undirected.emplace_back(rel.dstNodeIdx, rel.relLabels);
      dst.undirected.emplace_back(rel.srcNodeIdx, rel.relLabels);
    } else {
      src.outgoing.emplace_back(rel.dstNodeIdx, rel.relLabels);
      dst.incoming.emplace_back(rel.srcNodeIdx, rel.relLabels);
    }
  }
}

class Engine {
 public:
  void load(const std::string& schemaYaml, const std::string& stats) {
    auto schema = YAML::Load(schemaYaml);
    auto baselineMd = std::make_unique<Metadata>();
    auto indexMd = std::make_unique<Metadata>();
    baselineMd->updateSchema(schema);
    indexMd->updateSchema(schema);
    if (!stats.empty()) {
      baselineMd->updateStats(stats);
      indexMd->updateStats(stats);
    }
    auto baseline = std::make_unique<Context>(baselineMd.get());
    auto indexed = std::make_unique<Context>(indexMd.get());
    baseline_.reset();
    indexed_.reset();
    baselineMetadata_ = std::move(baselineMd);
    indexMetadata_ = std::move(indexMd);
    baseline_ = std::move(baseline);
    indexed_ = std::move(indexed);
    schemaYaml_ = schemaYaml;
    stats_ = stats;
    records_.clear();
    failures_.clear();
    for (const auto& candidate : candidates_) {
      try {
        registerCandidate(candidate);
      } catch (const std::exception& error) {
        failures_.emplace_back(candidate.id, error.what());
      }
    }
  }

  void loadIndexFile(const std::string& path) {
    Doc input;
    const auto contents = readFile(path);
    input.Parse(contents.data(), contents.size());
    if (input.HasParseError() || !input.IsObject())
      throw std::invalid_argument("Index file must be a JSON object");
    const auto& list = member(input, "indexes");
    if (!list.IsArray()) throw std::invalid_argument("\"indexes\" must be an array");
    for (const auto& definition : list.GetArray()) {
      auto candidate = parseCandidate(definition);
      try {
        add(candidate);
      } catch (const std::exception& error) {
        failures_.emplace_back(candidate.id, error.what());
      }
    }
  }

  std::string handle(const std::string& line) {
    Out out;
    Doc request;
    request.Parse(line.data(), line.size());
    std::string stage = "request";
    try {
      if (request.HasParseError() || !request.IsObject())
        throw std::invalid_argument("Request must be a JSON object");
      if (const auto& rid = member(request, "rid"); !rid.IsNull()) out.put("rid", J(rid, out.a()));
      const auto op = text(member(request, "op"), "op");
      if (op != "ping" && op != "load" && !indexed_) throw Failure("state", "No schema loaded");
      if (op == "ping") {
        out.put("protocol_version", J(PROTOCOL_VERSION));
        out.put("schema_loaded", J(indexed_ != nullptr));
      } else if (op == "load") {
        stage = "load";
        const auto& stats = member(request, "stats");
        load(text(member(request, "schema"), "schema"), stats.IsString() ? text(stats, "stats") : "");
        failuresJson(out);
        indexesJson(out);
      } else if (op == "register") {
        stage = "register";
        auto candidate = parseCandidate(request);
        const auto before = records_.size();
        add(candidate);
        J entries(rapidjson::kArrayType);
        for (size_t i = before; i < records_.size(); ++i) entries.PushBack(recordJson(out, records_[i]), out.a());
        out.put("entries", std::move(entries));
      } else if (op == "drop") {
        stage = "drop";
        out.put("dropped", J(uint64_t(drop(text(member(request, "candidate"), "candidate")))));
      } else if (op == "indexes") {
        indexesJson(out);
        failuresJson(out);
      } else if (op == "describe_fill") {
        stage = "describe_fill";
        // Bind in a disposable catalog: FILL binding creates index metadata
        // and can append hidden aggregate-state columns to its source query.
        Metadata metadata;
        metadata.updateSchema(YAML::Load(schemaYaml_));
        Context context(&metadata);
        const auto parsed = parseSingle(text(member(request, "fill"), "fill"));
        if (parsed->getStatementType() != neug::common::StatementType::COPY_FROM)
          throw std::invalid_argument("Expected one FILL statement");
        const auto& copy = parsed->cast<neug::parser::CopyFrom>();
        if (!copy.createIndex()) throw std::invalid_argument("Expected FILL, not COPY");
        neug::binder::Binder binder(&context);
        const auto bound = binder.bind(*parsed);
        const auto& fill = bound->cast<neug::binder::BoundCopyFrom>();
        auto* catalog = context.getCatalog();
        auto* transaction = context.getTransaction();
        std::vector<std::string> columns;
        for (auto* entry : catalog->getExtendedIndexEntries(transaction)) {
          const auto& info = entry->getAuxInfoUnsafe().cast<neug::catalog::FillIndexAuxInfo>();
          if (!columns.empty() && columns != info.getTargetColumns())
            throw std::invalid_argument("FILL partitions have different target columns");
          columns = info.getTargetColumns();
        }
        out.put("target_columns", out.strings(columns));
        const auto* source = fill.getInfo()->source.get();
        if (source == nullptr || source->type != neug::common::ScanSourceType::QUERY) {
          // Variadic shortcuts may be catalog-only at bind time.
          bind(out, context, copy.getFillSourceQuery(), {});
        } else {
          const auto& query = source->constCast<neug::binder::BoundQueryScanSource>();
          cico::BoundIR ir(out.a());
          out.put("ir", ir.statement(*query.statement));
        }
      } else if (op == "rewrite") {
        stage = "parameter_types";
        const auto types = parameterTypes(member(request, "parameter_types"), *indexed_);
        rewrite(out, text(member(request, "cypher"), "cypher"), types);
      } else if (op == "bind") {
        const auto& which = member(request, "catalog");
        auto& context = which.IsString() && text(which, "catalog") == "indexed" ? *indexed_ : *baseline_;
        stage = "parameter_types";
        const auto types = parameterTypes(member(request, "parameter_types"), context);
        bind(out, context, text(member(request, "cypher"), "cypher"), types);
      } else {
        throw std::invalid_argument("Unknown op: " + op);
      }
      out.put("ok", J(true));
    } catch (const Failure& error) {
      out.put("ok", J(false));
      out.put("stage", error.stage);
      out.put("error", std::string(error.what()));
    } catch (const std::exception& error) {
      out.put("ok", J(false));
      out.put("stage", stage);
      out.put("error", std::string(error.what()));
    }
    return out.dump();
  }

  std::string startupReport() {
    Out out;
    out.put("ok", J(true));
    out.put("event", std::string("ready"));
    out.put("protocol_version", J(PROTOCOL_VERSION));
    out.put("schema_loaded", J(indexed_ != nullptr));
    if (indexed_) {
      indexesJson(out);
      failuresJson(out);
    }
    return out.dump();
  }

 private:
  static Candidate parseCandidate(const J& definition) {
    Candidate candidate;
    candidate.id = text(member(definition, "id"), "index id");
    if (candidate.id.empty()) throw std::invalid_argument("Index id must be nonempty");
    const auto& fills = member(definition, "fills");
    if (!fills.IsArray() || fills.Empty()) throw std::invalid_argument("fills must be a nonempty array");
    for (const auto& fill : fills.GetArray()) candidate.fills.push_back(text(fill, "fill"));
    return candidate;
  }

  void add(const Candidate& candidate) {
    for (const auto& existing : candidates_)
      if (existing.id == candidate.id) throw std::invalid_argument("Duplicate index id: " + candidate.id);
    if (!indexed_) throw Failure("state", "No schema loaded");
    registerCandidate(candidate);
    candidates_.push_back(candidate);
  }

  size_t drop(const std::string& id) {
    auto* catalog = indexed_->getCatalog();
    auto* transaction = indexed_->getTransaction();
    size_t dropped = 0;
    for (auto it = records_.begin(); it != records_.end();) {
      if (it->candidate != id) { ++it; continue; }
      for (const auto* entry : catalog->getExtendedIndexEntries(transaction))
        if (entry->getName() == it->name) {
          catalog->dropExtendedIndex(transaction, entry->getTableID(), entry->getName());
          break;
        }
      it = records_.erase(it);
      ++dropped;
    }
    std::erase_if(candidates_, [&](const Candidate& c) { return c.id == id; });
    std::erase_if(failures_, [&](const auto& f) { return f.first == id; });
    return dropped;
  }

  // All branches of a candidate register, or none does.
  void registerCandidate(const Candidate& candidate) {
    auto* catalog = indexed_->getCatalog();
    auto* transaction = indexed_->getTransaction();
    std::set<std::string> previous;
    for (const auto* entry : catalog->getExtendedIndexEntries(transaction)) previous.insert(entry->getName());
    std::vector<IndexRecord> added;
    try {
      for (const auto& fill : candidate.fills) {
        auto parsed = neug::parser::Parser::parseQuery(fill);
        if (parsed.size() != 1 || parsed[0]->getStatementType() != neug::common::StatementType::COPY_FROM)
          throw std::invalid_argument("Each fills item must be exactly one FILL statement");
        const auto& copy = parsed[0]->cast<neug::parser::CopyFrom>();
        if (!copy.createIndex() || copy.getFillSourceQuery().empty())
          throw std::invalid_argument("Ordinary COPY is not a FILL definition");
        const auto before = catalog->getExtendedIndexEntries(transaction).size();
        neug::binder::Binder binder(indexed_.get());
        binder.bind(*parsed[0]);
        if (catalog->getExtendedIndexEntries(transaction).size() <= before)
          throw std::invalid_argument("FILL bound without registering an index");
      }
      for (auto* entry : catalog->getExtendedIndexEntries(transaction)) {
        if (previous.contains(entry->getName())) continue;
        auto& info = entry->getAuxInfoUnsafe().cast<neug::catalog::FillIndexAuxInfo>();
        normalizeFillPartitions(info, *catalog, transaction);
        IndexRecord record;
        record.candidate = candidate.id;
        record.name = entry->getName();
        record.table = info.getTableName();
        record.fillType = info.getFillType() == neug::catalog::FillEntryType::REL ? "REL" : "NODE";
        record.sourceQuery = info.getSourceQuery();
        record.columns = info.getTargetColumns();
        added.push_back(std::move(record));
      }
      // READY is simulated only in this nonpersistent compiler catalog.
      for (const auto& record : added) catalog->markExtendedIndexReady(record.name);
      records_.insert(records_.end(), added.begin(), added.end());
    } catch (...) {
      for (const auto* entry : catalog->getExtendedIndexEntries(transaction))
        if (!previous.contains(entry->getName()))
          catalog->dropExtendedIndex(transaction, entry->getTableID(), entry->getName());
      throw;
    }
  }

  static Context::ParameterTypes parameterTypes(const J& declarations, Context& context) {
    Context::ParameterTypes result;
    if (declarations.IsNull()) return result;
    if (!declarations.IsObject()) throw std::invalid_argument("parameter_types must be an object");
    for (auto it = declarations.MemberBegin(); it != declarations.MemberEnd(); ++it) {
      const auto name = text(it->name, "parameter name");
      const auto type = text(it->value, "parameter type");
      if (name.empty() || result.contains(name))
        throw std::invalid_argument("Parameter declarations must have unique nonempty names");
      result.emplace(name, type == "ANY" ? neug::common::LogicalType::ANY()
                                         : neug::common::LogicalType::convertFromString(type, &context));
    }
    return result;
  }

  static void columnsJson(Out& out, const neug::main::PreparedStatement& prepared) {
    J columns(rapidjson::kArrayType);
    const auto* result = prepared.getStatementResult();
    if (result != nullptr && prepared.logicalPlan && !prepared.logicalPlan->isEmpty()) {
      const auto& plan = *prepared.logicalPlan;
      neug::gopt::GAliasManager aliases(plan);
      const auto expressions = plan.getOutputExpressions();
      const auto names = result->getColumnNames();
      for (size_t i = 0; i < expressions.size(); ++i) {
        const auto& expression = expressions[i];
        const auto alias = aliases.getGAliasName(aliases.getAliasId(expression->getUniqueName()));
        const auto name = alias.queryName.has_value()
            ? (names.size() == expressions.size() ? names[i] : alias.queryName.value())
            : alias.uniqueName;
        J column(rapidjson::kArrayType);
        column.PushBack(out.str(name), out.a());
        column.PushBack(out.str(expression->getDataType().toString()), out.a());
        columns.PushBack(column, out.a());
      }
    }
    out.put("columns", std::move(columns));
  }

  static std::shared_ptr<neug::parser::Statement> parseSingle(const std::string& cypher) {
    auto parsed = neug::parser::Parser::parseQuery(cypher);
    if (parsed.size() != 1) throw Failure("parse", "Expected exactly one statement");
    return parsed[0];
  }

  using Bound = std::unique_ptr<neug::binder::BoundStatement>;

  static Bound bindWith(Context& context, const neug::parser::Statement& statement,
                        const Context::ParameterTypes& types,
                        const std::vector<std::string>* replay, bool split,
                        std::vector<std::string>* next = nullptr, bool* splitCandidate = nullptr) {
    neug::binder::Binder binder(&context);
    if (replay) binder.setIndexRewriteReplay(*replay);
    if (split) binder.enableVariadicAnchorSplit();
    binder.setParameterTypes(types);
    auto bound = binder.bind(statement);
    if (replay && !binder.indexRewriteReplayComplete()) return nullptr;
    if (next) *next = binder.getIndexRewriteNextChoices();
    if (splitCandidate) *splitCandidate = binder.hasVariadicAnchorSplitCandidate();
    return bound;
  }

  static bool sameV1(const neug::binder::BoundStatement& statement, Context& context, const J& target) {
    Doc candidate;
    const auto json = neug::binder::structuredBoundStatementJson(statement, context);
    candidate.Parse(json.data(), json.size());
    return !candidate.HasParseError() && candidate == target;
  }

  // Re-bind the parsed query along candidate routes until one reproduces
  // NeuG's trace of the selected statement exactly.
  static Bound reproduce(Context& context, const neug::parser::Statement& statement,
                         const Context::ParameterTypes& types, const J& target,
                         std::string& route, uint64_t& binds) {
    std::vector<std::string> next;
    bool splitCandidate = false;
    ++binds;
    if (auto plain = bindWith(context, statement, types, nullptr, false, nullptr, &splitCandidate);
        plain && sameV1(*plain, context, target)) {
      route = "initial";
      return plain;
    }
    if (splitCandidate) {
      ++binds;
      if (auto split = bindWith(context, statement, types, nullptr, true); split && sameV1(*split, context, target)) {
        route = "variadic_split";
        return split;
      }
    }
    std::deque<std::vector<std::string>> pending{{}};
    std::set<std::vector<std::string>> seen{{}};
    while (!pending.empty() && binds < MAX_REPLAY_BINDS) {
      auto prefix = std::move(pending.front());
      pending.pop_front();
      ++binds;
      Bound bound;
      try {
        bound = bindWith(context, statement, types, &prefix, false, &next);
      } catch (const std::exception&) {
        continue;
      }
      if (!bound) continue;
      if (sameV1(*bound, context, target)) {
        route = "replay:" + std::to_string(prefix.size());
        return bound;
      }
      for (const auto& choice : next) {
        if (std::find(prefix.begin(), prefix.end(), choice) != prefix.end()) continue;
        auto successor = prefix;
        successor.push_back(choice);
        if (seen.insert(successor).second) pending.push_back(std::move(successor));
      }
    }
    return nullptr;
  }

  void rewrite(Out& out, const std::string& cypher, const Context::ParameterTypes& types) {
    auto& context = *indexed_;
    std::unique_ptr<neug::main::PreparedStatement> prepared;
    std::string trace;
    {
      neug::binder::BoundRewriteTrace recorder;
      prepared = context.prepare(cypher, types);
      trace = recorder.toJson();
    }
    if (!prepared->isSuccess()) throw Failure("prepare", prepared->getErrorMessage());
    out.put("read_only", J(prepared->isReadOnly()));
    out.put("cost_compared", J(prepared->indexRewriteCostCompared));
    out.put("original_work", J(prepared->indexRewriteOriginalWork));
    out.put("selected_work", J(prepared->indexRewriteSelectedWork));
    out.put("alternatives", J(prepared->indexRewriteAlternatives));
    columnsJson(out, *prepared);
    Doc traceDoc;
    traceDoc.Parse(trace.data(), trace.size());
    const auto& before = member(traceDoc, "before");
    const auto& after = member(traceDoc, "after");
    J steps(rapidjson::kArrayType);
    if (const auto& list = member(traceDoc, "steps"); list.IsArray())
      for (const auto& step : list.GetArray())
        steps.PushBack(out.str(text(member(member(step, "index"), "name"), "index name")), out.a());
    out.put("index_entries", std::move(steps));
    if (after.IsNull()) throw Failure("trace", "NeuG did not trace a bound statement");
    out.put("changed", J(!(before == after)));
    if (before == after || !prepared->isReadOnly()) return;
    const auto statement = parseSingle(cypher);
    std::string route;
    uint64_t binds = 0;
    auto selected = reproduce(context, *statement, types, after, route, binds);
    out.put("reproduction_binds", J(binds));
    if (!selected) throw Failure("reproduce", "Could not reproduce the selected bound statement");
    out.put("route", route);
    cico::BoundIR ir(out.a());
    out.put("ir", ir.statement(*selected));
  }

  static void bind(Out& out, Context& context, const std::string& cypher,
                   const Context::ParameterTypes& types) {
    auto prepared = context.prepare(cypher, types);
    if (!prepared->isSuccess()) throw Failure("prepare", prepared->getErrorMessage());
    out.put("read_only", J(prepared->isReadOnly()));
    columnsJson(out, *prepared);
    const auto statement = parseSingle(cypher);
    auto bound = bindWith(context, *statement, types, nullptr, false);
    cico::BoundIR ir(out.a());
    out.put("ir", ir.statement(*bound));
  }

  J recordJson(Out& out, const IndexRecord& r) {
    J o(rapidjson::kObjectType);
    out.put(o, "candidate", out.str(r.candidate));
    out.put(o, "name", out.str(r.name));
    out.put(o, "target_table", out.str(r.table));
    out.put(o, "fill_type", out.str(r.fillType));
    out.put(o, "target_columns", out.strings(r.columns));
    out.put(o, "source_query", out.str(r.sourceQuery));
    return o;
  }

  void indexesJson(Out& out) {
    J list(rapidjson::kArrayType);
    for (const auto& r : records_) list.PushBack(recordJson(out, r), out.a());
    out.put("indexes", std::move(list));
  }

  void failuresJson(Out& out) {
    J failures(rapidjson::kArrayType);
    for (const auto& [id, error] : failures_) {
      J f(rapidjson::kObjectType);
      out.put(f, "candidate", out.str(id));
      out.put(f, "error", out.str(error));
      failures.PushBack(f, out.a());
    }
    out.put("failed_candidates", std::move(failures));
  }

  std::unique_ptr<Metadata> baselineMetadata_, indexMetadata_;
  std::unique_ptr<Context> baseline_, indexed_;
  std::string schemaYaml_, stats_;
  std::vector<Candidate> candidates_;
  std::vector<IndexRecord> records_;
  std::vector<std::pair<std::string, std::string>> failures_;
};
}  // namespace

int main(int argc, char** argv) {
  std::map<std::string, std::string> options;
  bool compileOnly = false;
  for (int i = 1; i < argc; ++i) {
    const std::string option = argv[i];
    if (option == "--compile-only") {
      compileOnly = true;
    } else if ((option == "--schema" || option == "--indexes" || option == "--stats") && i + 1 < argc) {
      options[option] = argv[++i];
    } else {
      std::cerr << "Unknown or incomplete argument: " << option << '\n';
      return 2;
    }
  }
  if (!compileOnly) {
    std::cerr << "Usage: incgq_rewrite_engine --compile-only [--schema S.yaml [--stats st.json] "
                 "[--indexes I.json]]\n";
    return 2;
  }
  const int protocolFd = dup(STDOUT_FILENO);
  if (protocolFd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
    std::perror("incgq_rewrite_engine: cannot redirect stdout");
    return 1;
  }
  FILE* protocol = fdopen(protocolFd, "w");
  const auto send = [&](const std::string& line) {
    std::fputs(line.c_str(), protocol);
    std::fputc('\n', protocol);
    std::fflush(protocol);
  };
  Engine engine;
  try {
    if (options.contains("--schema")) {
      engine.load(readFile(options["--schema"]),
                  options.contains("--stats") ? readFile(options["--stats"]) : "");
      if (options.contains("--indexes")) engine.loadIndexFile(options["--indexes"]);
    }
  } catch (const std::exception& error) {
    Out out;
    out.put("ok", J(false));
    out.put("event", std::string("startup_failed"));
    out.put("error", std::string(error.what()));
    send(out.dump());
    return 1;
  }
  send(engine.startupReport());
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    send(engine.handle(line));
  }
  return 0;
}
