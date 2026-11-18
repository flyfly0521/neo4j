#!/bin/bash
# Clone a Neo4j store (data + conf) and start a private server on it.
#   tests/neo4j_clone.sh SRC_STORE DST_DIR BOLT_PORT [HEAP]
#   stop:    NEO4J_CONF=DST_DIR/conf $NEO4J_HOME/bin/neo4j stop
#   restart without copying: NO_COPY=1 tests/neo4j_clone.sh SRC DST PORT HEAP
# SRC is only read.  NEO4J_HOME / JAVA_HOME default to the lab installation.
set -euo pipefail
SRC=$1; DST=$2; PORT=$3; HEAP=${4:-4g}
NEO4J_HOME=${NEO4J_HOME:-/mnt/sdb1/dwk/imported_data/neo4j/neo4j-5.26.28/tools/neo4j-community-5.26.28}
JAVA_HOME=${JAVA_HOME:-/mnt/sdb1/dwk/imported_data/neo4j/neo4j-5.26.28/tools/jdk-21.0.12+8-jre}
if [ -z "${NO_COPY:-}" ]; then
  rm -rf "$DST"; mkdir -p "$DST/conf" "$DST/logs" "$DST/run" "$DST/import"
  cp -r "$SRC/data" "$DST/data"
  rm -f "$DST/data/databases/store_lock"
fi
cat > "$DST/conf/neo4j.conf" <<CONF
server.directories.data=$DST/data
server.directories.logs=$DST/logs
server.directories.run=$DST/run
server.directories.import=$DST/import
server.default_listen_address=127.0.0.1
server.bolt.listen_address=127.0.0.1:$PORT
server.http.enabled=false
server.https.enabled=false
dbms.security.auth_enabled=false
server.memory.heap.initial_size=$HEAP
server.memory.heap.max_size=$HEAP
server.memory.pagecache.size=$HEAP
db.memory.transaction.total.max=0
db.tx_log.rotation.retention_policy=1 files
server.jvm.additional=--add-opens=java.base/java.nio=ALL-UNNAMED
CONF
export JAVA_HOME NEO4J_CONF="$DST/conf"
"$NEO4J_HOME/bin/neo4j" stop > /dev/null 2>&1 || true
"$NEO4J_HOME/bin/neo4j" start > "$DST/console.log" 2>&1
for i in $(seq 1 120); do
  if grep -q "Started." "$DST/logs/neo4j.log" 2>/dev/null; then echo "neo4j up on bolt://127.0.0.1:$PORT"; exit 0; fi
  sleep 2
done
echo "timeout waiting for neo4j"; tail -n 20 "$DST/logs/neo4j.log" "$DST/console.log"; exit 1
