-- profile_persistence_health.sql — read-only persistence-loop SLO queries
--
-- Run via: python scripts/diagnostics/db_query.py profile_persistence_health.sql
-- Or:      python scripts/diagnostics/db_query.py profile_persistence_health.sql --query Q4
--
-- Each query is delimited by a -- :: name comment so the runner can pick a
-- single named query rather than executing the entire file.
--
-- All queries are read-only SELECTs. The runner enforces this with a token
-- check before execution; if you add a query, keep it SELECT-only. Schema
-- changes belong in alembic migrations, not here.
--
-- Cross-references:
--   - Architecture map of the 5 channels: memory file
--     project_self_learning_loop_arch.md
--   - PR 1 added MAPPING_SAVE_DROPPED + PROFILE_UPDATE_FAILED events; the
--     event side of the dashboard reads them from events.jsonl, this file
--     covers the DB side.

-- :: Q4_channel_row_counts
-- Writer asymmetry diagnostic. When any two channels differ by >100x in
-- profile-with-data count and they share update_profile_after_extraction
-- as their writer, a writer is broken. PR 1 (mappings) + PR 2 (patches)
-- are the two channel repairs that closed the gap as of 2026-05-10.
SELECT
  count(*)                                                                  AS total_profiles,
  count(*) FILTER (
    WHERE jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'llm_field_mappings', '[]'::jsonb)
    ) > 0
  )                                                                         AS profiles_with_mappings,
  count(*) FILTER (
    WHERE jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'field_patches', '[]'::jsonb)
    ) > 0
  )                                                                         AS profiles_with_patches,
  count(*) FILTER (
    WHERE jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'blocked_endpoints', '[]'::jsonb)
    ) > 0
  )                                                                         AS profiles_with_blocked,
  count(*) FILTER (
    WHERE jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'known_endpoints', '[]'::jsonb)
    ) > 0
  )                                                                         AS profiles_with_known,
  count(*) FILTER (
    WHERE COALESCE(
      (payload::jsonb)->'dom_hints'->'field_selectors'->>'container', ''
    ) <> ''
  )                                                                         AS profiles_with_dom_selectors,
  COALESCE(SUM(jsonb_array_length(
    COALESCE((payload::jsonb)->'api_hints'->'llm_field_mappings', '[]'::jsonb)
  )), 0)                                                                    AS total_mapping_entries,
  COALESCE(SUM(jsonb_array_length(
    COALESCE((payload::jsonb)->'api_hints'->'field_patches', '[]'::jsonb)
  )), 0)                                                                    AS total_patch_entries
FROM scrape_profiles;

-- :: Q5_recent_activity
-- When was each channel last updated? If any channel's max(updated_at) is
-- > 24h old AND the runner has been running daily, the writer probably
-- regressed. Useful for "did PR-1 / PR-2 actually take effect?" probes.
SELECT
  count(*)                                                       AS total_profiles,
  count(*) FILTER (WHERE updated_at >= now() - INTERVAL '24 hours')   AS updated_last_24h,
  count(*) FILTER (WHERE updated_at >= now() - INTERVAL '7 days')    AS updated_last_7d,
  count(*) FILTER (WHERE updated_by = 'BOOTSTRAP')                   AS bootstrapped_only,
  count(*) FILTER (WHERE updated_by = 'LLM_EXTRACTION')              AS updated_by_llm,
  max(updated_at)                                                    AS most_recent_update,
  min(updated_at)                                                    AS oldest_update
FROM scrape_profiles;

-- :: Q6_maturity_distribution
-- Sanity check on profile maturity rollover. Pre-PR-1 the loop was so
-- broken that profiles couldn't promote past WARM (no replay → no
-- consecutive_successes accumulation). After PR 1+2, HOT count should
-- grow over a few daily runs as profile_replay starts hitting.
SELECT
  COALESCE((payload::jsonb)->'confidence'->>'maturity', 'unknown') AS maturity,
  count(*) AS n
FROM scrape_profiles
GROUP BY maturity
ORDER BY n DESC;

-- :: Q7_top_replay_winners
-- Profiles with the most accumulated replay-success count across saved
-- mappings + patches. These are the cases where the self-learning loop
-- has worked — useful as positive examples and for a "champions" report.
--
-- The CTE first computes per-profile aggregates (so GROUP BY can use
-- canonical_id alone — the underlying ``payload`` column is `json`
-- which has no equality operator and can't be grouped on directly).
WITH per_profile AS (
  SELECT
    canonical_id,
    updated_at,
    jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'llm_field_mappings', '[]'::jsonb)
    ) AS n_mappings,
    jsonb_array_length(
      COALESCE((payload::jsonb)->'api_hints'->'field_patches', '[]'::jsonb)
    ) AS n_patches,
    (payload::jsonb)->'api_hints'->'llm_field_mappings' AS mappings_jsonb
  FROM scrape_profiles
),
mapping_success AS (
  SELECT
    canonical_id,
    COALESCE(SUM((m.value->>'success_count')::int), 0) AS mapping_success_total
  FROM per_profile
  LEFT JOIN LATERAL jsonb_array_elements(
    COALESCE(mappings_jsonb, '[]'::jsonb)
  ) AS m ON TRUE
  GROUP BY canonical_id
)
SELECT
  pp.canonical_id,
  ms.mapping_success_total,
  pp.n_mappings,
  pp.n_patches,
  pp.updated_at
FROM per_profile pp
JOIN mapping_success ms USING (canonical_id)
WHERE ms.mapping_success_total > 0
ORDER BY ms.mapping_success_total DESC
LIMIT 25;
