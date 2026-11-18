// Q4. Top message creators in a country
// Kuzu adaptation: rewrites Neo4j CALL/UNION into an OPTIONAL MATCH over top forum members.
MATCH (country:PLACE)<-[:ISPARTOF]-(city:PLACE)<-[:ISLOCATEDIN]-(member:PERSON)<-[:HASMEMBER]-(forum:FORUM)
WHERE country.type = 'Country'
  AND city.type = 'City'
  AND CAST(forum.creationDate AS TIMESTAMP) > CAST($date AS TIMESTAMP)
WITH country, forum, count(member) AS numberOfMembers
WITH forum, max(numberOfMembers) AS numberOfMembers
ORDER BY numberOfMembers DESC, forum.id ASC
LIMIT 100
WITH collect(forum) AS topForums
UNWIND topForums AS memberForum
MATCH (person:PERSON)<-[:HASMEMBER]-(memberForum)
WITH DISTINCT topForums, person
// Same result as OPTIONAL MATCH ... (rootForum:FORUM) WHERE rootForum IN topForums:
// every row has a non-empty topForums (person comes from UNWIND topForums), FORUM.id
// is the primary key, and count(DISTINCT message) ignores the per-forum duplication.
// Written per forum so that the index rewrite stays inside one QueryPart (IncGQ
// middleware); Kuzu cannot bind an unwound node in a pattern, hence the id.
UNWIND topForums AS topForum
WITH person, topForum.id AS rootForumId
OPTIONAL MATCH
  (person)<-[:HASCREATOR]-(message:POST:COMMENT)-[:REPLYOF*0..]->
  (post:POST)<-[:CONTAINEROF]-(rootForum:FORUM {id: rootForumId})
WITH person, count(DISTINCT message) AS messageCount
RETURN
  person.id AS personId,
  person.firstName AS personFirstName,
  person.lastName AS personLastName,
  person.creationDate AS personCreationDate,
  messageCount
ORDER BY messageCount DESC, personId ASC
LIMIT 100
