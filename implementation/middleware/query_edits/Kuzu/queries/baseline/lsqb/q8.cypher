// Single QueryPart form of the original count{Post branch} + count{Comment branch}:
// every relationship table has fixed endpoint labels, so the Post:Comment pattern
// matches exactly the disjoint union of the two branches.  MATCH (d:Person) ...
// LIMIT 1 keeps the original row semantics (one row iff Person is non-empty).
MATCH (tag1:Tag)<-[:Post_hasTag_Tag|Comment_hasTag_Tag]-(message:Post:Comment)<-[:Comment_replyOf_Post|Comment_replyOf_Comment]-(comment:Comment)-[:Comment_hasTag_Tag]->(tag2:Tag)
WHERE NOT EXISTS { MATCH (comment)-[:Comment_hasTag_Tag]->(tag1) }
  AND tag1 <> tag2
WITH count(*) AS cnt
MATCH (d:Person)
WITH cnt, d LIMIT 1
RETURN cnt AS count
