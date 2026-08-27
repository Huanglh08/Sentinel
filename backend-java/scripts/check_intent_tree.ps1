$query = "SELECT intent_code, name, level, parent_code, kind, enabled FROM t_intent_node WHERE intent_code LIKE 'kb-%' AND deleted=0 ORDER BY level, intent_code;"
docker exec postgres psql -U postgres -d ragent -c $query
