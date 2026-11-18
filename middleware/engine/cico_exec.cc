// Test-only executor: runs Cypher statements against a NeuG database so the
// middleware's rewritten output can be compared with the original query.
//
//   cico_exec --db DIR      (JSON lines on stdin, one response per line on stdout)
//   request:  {"id":..,"cypher":"...","mode":"read|update|insert|schema",
//              "params":{...},"disable_rewrite":true}
//   response: {"id":..,"ok":true,"rows":["<row>", ...]} | {"ok":false,"error":"..."}
//
// disable_rewrite toggles NEUG_DISABLE_INDEX_REWRITE for that statement, so a
// printed rewrite is executed exactly as written.

#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>

#include "glog/logging.h"
#include "neug/main/connection.h"
#include "neug/main/neug_db.h"
#include "neug/main/query_result.h"

int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]);
  std::string dir;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--db" && i + 1 < argc) dir = argv[++i];
  }
  if (dir.empty()) {
    std::cerr << "Usage: cico_exec --db DIR\n";
    return 2;
  }
  const int protocolFd = dup(STDOUT_FILENO);
  dup2(STDERR_FILENO, STDOUT_FILENO);
  FILE* protocol = fdopen(protocolFd, "w");
  neug::NeugDB db;
  if (!db.Open(dir, 1)) {
    std::fputs("{\"ok\":false,\"event\":\"open_failed\"}\n", protocol);
    return 1;
  }
  auto conn = db.Connect();
  std::fputs("{\"ok\":true,\"event\":\"ready\"}\n", protocol);
  std::fflush(protocol);
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    rapidjson::Document request, out;
    out.SetObject();
    auto& a = out.GetAllocator();
    request.Parse(line.data(), line.size());
    try {
      if (request.HasParseError() || !request.IsObject()) throw std::runtime_error("bad request");
      if (request.HasMember("id")) out.AddMember("id", rapidjson::Value(request["id"], a), a);
      const std::string cypher = request["cypher"].GetString();
      const std::string mode = request.HasMember("mode") ? request["mode"].GetString() : "read";
      const bool disable = request.HasMember("disable_rewrite") && request["disable_rewrite"].GetBool();
      if (disable) setenv("NEUG_DISABLE_INDEX_REWRITE", "1", 1);
      else unsetenv("NEUG_DISABLE_INDEX_REWRITE");
      rapidjson::Value empty(rapidjson::kObjectType);
      const auto& params = request.HasMember("params") ? request["params"] : empty;
      auto result = conn->Query(cypher, mode, params);
      unsetenv("NEUG_DISABLE_INDEX_REWRITE");
      if (!result) throw std::runtime_error(result.error().ToString());
      rapidjson::Value rows(rapidjson::kArrayType);
      for (auto row : result.value()) {
        const auto s = row.ToString();
        rows.PushBack(rapidjson::Value(s.data(), s.size(), a), a);
      }
      out.AddMember("rows", rows, a);
      out.AddMember("ok", true, a);
    } catch (const std::exception& e) {
      out.AddMember("ok", false, a);
      out.AddMember("error", rapidjson::Value(e.what(), a), a);
    }
    rapidjson::StringBuffer buffer;
    rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
    out.Accept(writer);
    std::fputs(buffer.GetString(), protocol);
    std::fputc('\n', protocol);
    std::fflush(protocol);
  }
  conn->Close();
  db.Close();
  return 0;
}
