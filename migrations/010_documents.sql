-- PDFs: the file itself (kept in the database for now), its extracted text,
-- and that text in searchable chunks so questions can be answered from it.
ALTER TABLE attachments ALTER COLUMN s3_key DROP NOT NULL;
ALTER TABLE attachments ADD COLUMN filename   text;
ALTER TABLE attachments ADD COLUMN data       bytea;              -- the original file
ALTER TABLE attachments ADD COLUMN sha256     text;
ALTER TABLE attachments ADD COLUMN pages      integer;
ALTER TABLE attachments ADD COLUMN extraction text CHECK (extraction IN ('text', 'vision', 'none'));
ALTER TABLE attachments ADD COLUMN text       text;               -- extracted text (or AI reading of a scan)
ALTER TABLE attachments ADD COLUMN record_id  uuid REFERENCES records(id) ON DELETE SET NULL;
CREATE INDEX attachments_by_inbox ON attachments (inbox_id);
CREATE INDEX attachments_by_record ON attachments (record_id);

CREATE TABLE attachment_chunks (
    id             bigserial PRIMARY KEY,
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    attachment_id  uuid NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    seq            integer NOT NULL,
    page           integer,
    content        text NOT NULL,
    search         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX attachment_chunks_search ON attachment_chunks USING gin (search);
CREATE INDEX attachment_chunks_by_tenant ON attachment_chunks (tenant_id);

ALTER TABLE attachment_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON attachment_chunks USING (atya_visible(tenant_id)) WITH CHECK (atya_visible(tenant_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON attachment_chunks TO atya_app;
GRANT USAGE, SELECT ON SEQUENCE attachment_chunks_id_seq TO atya_app;
