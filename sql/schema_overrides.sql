-- Schema overrides, applied AFTER the brief's schema.sql, BEFORE the ETL load.
--
-- The brief's schema.sql is kept byte-identical to the challenge repo. The one
-- place the live dataset cannot satisfy that schema is the rate_plan_code foreign
-- key: reservations book against 16 granular rate codes (e.g. EXPP, BARCBB,
-- BOOKBARB), while the published rate_plan_lookup is a fixed 8-row reference
-- dimension, and /verify plus ETL test scenario 1 both REQUIRE exactly 8 rows.
-- A strict FK + an 8-row lookup + loading every real rate code cannot all hold.
--
-- Decision: keep the real rate_plan_code on the fact table (it is needed for the
-- pricing/commercial questions in the brief) and treat rate_plan_lookup as a
-- partial descriptive dimension rather than an enforced parent. We therefore drop
-- ONLY this one FK. The other three dimensions (space_type, market_code,
-- channel_code) reference their lookups cleanly and are left fully enforced.
-- See ATTESTATION.md / ARCHITECTURE.md for the rationale.

alter table public.reservations_hackathon
  drop constraint if exists reservations_hackathon_rate_plan_code_fkey;
