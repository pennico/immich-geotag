-- cluster_matches.sql: visually similar located photos for every
-- unlocated image, via Immich's CLIP embeddings (smart_search, pgvector).
--
-- Run on the homelab host (read-only):
--   docker exec -i immich_postgres psql -U postgres -d immich -At \
--     < cluster_matches.sql > matches.csv
--
-- Output (CSV, no header, positional - see cluster_fill.py MATCH_COLUMNS):
--   asset_id, file_name, match_id, match_name, similarity, latitude, longitude
--
-- For every IMAGE asset without GPS, the top 10 located images with
-- cosine similarity >= 0.85 (distance < 0.15). The <=> operator uses the
-- ANN index when present; the distance filter keeps the result small.

COPY (
  WITH located AS (
    SELECT s."assetId" AS id, a."originalFileName" AS name,
           e.latitude AS lat, e.longitude AS lon, s.embedding AS emb
    FROM smart_search s
    JOIN asset a ON a.id = s."assetId"
    JOIN asset_exif e ON e."assetId" = s."assetId"
    WHERE a.type = 'IMAGE' AND a."deletedAt" IS NULL
      AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
  ),
  unlocated AS (
    SELECT s."assetId" AS id, a."originalFileName" AS name, s.embedding AS emb
    FROM smart_search s
    JOIN asset a ON a.id = s."assetId"
    LEFT JOIN asset_exif e ON e."assetId" = a.id
    WHERE a.type = 'IMAGE' AND a."deletedAt" IS NULL
      AND (e.latitude IS NULL OR e.longitude IS NULL)
  )
  SELECT u.id, u.name, m.id, m.name,
         round((1 - (m.emb <=> u.emb))::numeric, 4),
         m.lat, m.lon
  FROM unlocated u
  CROSS JOIN LATERAL (
    SELECT l.id, l.name, l.lat, l.lon, l.emb
    FROM located l
    WHERE l.emb <=> u.emb < 0.15
    ORDER BY l.emb <=> u.emb
    LIMIT 10
  ) m
  ORDER BY u.id, 5 DESC
) TO STDOUT WITH (FORMAT csv);
