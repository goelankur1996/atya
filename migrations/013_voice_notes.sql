-- Voice notes: the audio is kept as an attachment, its transcript in
-- attachments.text (extraction 'transcript'). The inbox row keeps a fixed
-- placeholder ("[Voice note 0:42]") because inbox content never changes.
ALTER TABLE attachments DROP CONSTRAINT IF EXISTS attachments_extraction_check;
ALTER TABLE attachments ADD CONSTRAINT attachments_extraction_check
    CHECK (extraction IN ('text', 'vision', 'none', 'transcript'));
ALTER TABLE attachments ADD COLUMN seconds  integer;      -- length of an audio file, when known
ALTER TABLE attachments ADD COLUMN language text;         -- language the transcript was made in
