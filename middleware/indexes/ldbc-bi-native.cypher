// Editable registration file: NeuG DDL/FILL input, Neo4j target output.
// Sources: IncGQ-experiment-docs commit 08f1a2659ea2304b7940fd15db3c23382bf37eb7.
// Static definitions only; parameterized indexes must use fixed/bound parameters.
// Managed loading builds only definitions without a matching successful receipt.
// DDL here changes the middleware schema; it is never sent verbatim to Neo4j.


// Source: NeuG/queries/index/ldbc-bi/bi10/index.cypher
// Shared with BI3: build this countryName definition once per database.
// Baseline queries use location paths; the compiler substitutes the point index.
ALTER TABLE PERSON ADD IF NOT EXISTS countryName STRING DEFAULT '';
FILL PERSON(countryName) FROM (
MATCH (country:PLACE)<-[:ISPARTOF]-(:PLACE)<-[:ISLOCATEDIN]-(person:PERSON)
RETURN person.id, country.name);


// Source: NeuG/queries/index/ldbc-bi/bi9/index.cypher
DROP TABLE IF EXISTS ROOT_POST;
CREATE REL TABLE IF NOT EXISTS ROOT_POST(from Comment to Post, from Post to Post);
FILL ROOT_POST() FROM (
  MATCH (post:Post)<-[e:REPLYOF*0..4294967295]-(message:COMMENT:POST)
  RETURN DISTINCT message.id, post.id
);


// Source: NeuG/queries/index/ldbc-bi/bi5/index.cypher
ALTER TABLE COMMENT ADD IF NOT EXISTS likeCount INT64 DEFAULT 0;
ALTER TABLE POST ADD IF NOT EXISTS likeCount INT64 DEFAULT 0;
ALTER TABLE COMMENT ADD IF NOT EXISTS replyCount INT64 DEFAULT 0;
ALTER TABLE POST ADD IF NOT EXISTS replyCount INT64 DEFAULT 0;
FILL COMMENT(likeCount) FROM (
MATCH (message:COMMENT)<-[:LIKES]-(liker:PERSON)
RETURN message.id, count(liker));
FILL POST(likeCount) FROM (
MATCH (message:POST)<-[:LIKES]-(liker:PERSON)
RETURN message.id, count(liker));
FILL COMMENT(replyCount) FROM (
MATCH (message:COMMENT)<-[:REPLYOF]-(reply:COMMENT)
RETURN message.id, count(reply));
FILL POST(replyCount) FROM (
MATCH (message:POST)<-[:REPLYOF]-(reply:COMMENT)
RETURN message.id, count(reply));


// Source: NeuG/queries/index/ldbc-bi/bi6/index.cypher
ALTER TABLE PERSON ADD IF NOT EXISTS likeCount INT64;
FILL PERSON(likeCount) FROM (
MATCH (person2)<-[:HASCREATOR]-(message2:COMMENT:POST)<-[like:LIKES]-(person3:Person)
RETURN person2.id, count(person3));
