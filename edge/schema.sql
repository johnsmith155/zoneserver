-- Field reports, aggregated. One row per hour, node, network type, mobile
-- operator and origin country; counters only. See src/index.js.
CREATE TABLE IF NOT EXISTS node_hour (
  bucket TEXT NOT NULL,            -- YYYY-MM-DDTHH, UTC
  node   TEXT NOT NULL,            -- the stable suffix of the published name
  net    TEXT NOT NULL,            -- cell | wifi | other
  op     TEXT NOT NULL,            -- mobile operator MCC-MNC, '' when not cellular
  cc     TEXT NOT NULL,            -- user's origin country
  ok     INTEGER NOT NULL DEFAULT 0,   -- connect verified
  hs     INTEGER NOT NULL DEFAULT 0,   -- never came up
  vf     INTEGER NOT NULL DEFAULT 0,   -- came up, carried nothing
  pok    INTEGER NOT NULL DEFAULT 0,   -- background check carried
  pfail  INTEGER NOT NULL DEFAULT 0,   -- background check failed
  ms_sum INTEGER NOT NULL DEFAULT 0,
  ms_n   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, node, net, op, cc)
);
CREATE INDEX IF NOT EXISTS node_hour_bucket_cc ON node_hour (bucket, cc);
