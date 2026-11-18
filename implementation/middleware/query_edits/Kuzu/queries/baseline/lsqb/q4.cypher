// One QueryPart per label branch of the original count{} + count{}:
// part 1 counts the Post branch; part 2 counts the Comment branch with
// OPTIONAL MATCH + count(message), which is 0 without a match (as count{}).
// MATCH (d:Person) ... LIMIT 1 keeps the original row semantics (one row iff
// Person is non-empty).
MATCH (:Tag)<-[:Post_hasTag_Tag]-(message:Post)-[:Post_hasCreator_Person]->(creator:Person),
  (message)<-[:Person_likes_Post]-(liker:Person),
  (message)<-[:Comment_replyOf_Post]-(comment:Comment)
WITH count(*) AS postCount
OPTIONAL MATCH (:Tag)<-[:Comment_hasTag_Tag]-(message:Comment)-[:Comment_hasCreator_Person]->(creator:Person),
  (message)<-[:Person_likes_Comment]-(liker:Person),
  (message)<-[:Comment_replyOf_Comment]-(comment:Comment)
WITH postCount, count(message) AS commentCount
MATCH (d:Person)
WITH postCount, commentCount, d LIMIT 1
RETURN postCount + commentCount AS count
