-- Where a row came from outside ATYA (e.g. 'notion:<page id>'). Used by the
-- Notion importer to map relations and to make re-runs skip what is already in.
ALTER TABLE inbox     ADD COLUMN external_id text;
ALTER TABLE records   ADD COLUMN external_id text;
ALTER TABLE outbox    ADD COLUMN external_id text;
ALTER TABLE audit_log ADD COLUMN external_id text;

CREATE UNIQUE INDEX inbox_external   ON inbox (tenant_id, external_id)     WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX records_external ON records (tenant_id, external_id)   WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX outbox_external  ON outbox (tenant_id, external_id)    WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX audit_external   ON audit_log (tenant_id, external_id) WHERE external_id IS NOT NULL;
