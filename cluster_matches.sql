-- cluster_matches.sql: visually similar located photos for every
-- unlocated image, via Immich's CLIP embeddings (smart_search, pgvector).
--
-- Run on the homelab host (read-only):
--   docker exec -i immich_postgres psql -U postgres -d immich -At \
--     < cluster_matches.sql > matches.csv
--
-- Output (CSV, no header, positional - see cluster_fill.py MATCH_COLUMNS):
--   asset_id, file_name, taken_at, match_id, match_name, match_taken_at,
--   similarity, latitude, longitude
--   (taken_at = epoch seconds from asset_exif."dateTimeOriginal", NULL
--    when the EXIF date is missing; used by the time-coherence rule)
--
-- For every IMAGE asset without GPS, the top 10 located images with
-- cosine similarity >= 0.85 (distance < 0.15). The <=> operator uses the
-- ANN index when present; the distance filter keeps the result small.

COPY (
  WITH located AS (
    SELECT s."assetId" AS id, a."originalFileName" AS name,
           e.latitude AS lat, e.longitude AS lon, s.embedding AS emb,
           extract(epoch FROM e."dateTimeOriginal")::bigint AS taken
    FROM smart_search s
    JOIN asset a ON a.id = s."assetId"
    JOIN asset_exif e ON e."assetId" = s."assetId"
    WHERE a.type = 'IMAGE' AND a."deletedAt" IS NULL
      AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
  ),
  unlocated AS (
    SELECT s."assetId" AS id, a."originalFileName" AS name, s.embedding AS emb,
           extract(epoch FROM e."dateTimeOriginal")::bigint AS taken
    FROM smart_search s
    JOIN asset a ON a.id = s."assetId"
    LEFT JOIN asset_exif e ON e."assetId" = a.id
    WHERE a.type = 'IMAGE' AND a."deletedAt" IS NULL
      AND (e.latitude IS NULL OR e.longitude IS NULL)
  )
  SELECT u.id, u.name, u.taken, m.id, m.name, m.taken,
         round((1 - (m.emb <=> u.emb))::numeric, 4),
         m.lat, m.lon
  FROM unlocated u
  CROSS JOIN LATERAL (
    SELECT l.id, l.name, l.lat, l.lon, l.emb, l.taken
    FROM located l
    WHERE l.emb <=> u.emb < 0.15
    ORDER BY l.emb <=> u.emb
    LIMIT 10
  ) m
  ORDER BY u.id, 5 DESC
) TO STDOUT WITH (FORMAT csv);
