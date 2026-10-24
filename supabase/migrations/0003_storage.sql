-- ---------------------------------------------------------------------------
-- E-SURI / IMECAR — storage buckets
--
-- Run after 0002_rls.sql.
--
-- Three buckets, all private. Every file here is a minor's work or a document
-- naming them, so nothing is world-readable: the API hands out a short-lived
-- signed URL (SIGNED_URL_TTL_SECONDS) when a specific person is entitled to a
-- specific file, and that link expires.
--
-- Bucket names must match backend/.env:
--   ANSWER_SHEET_BUCKET, CERTIFICATE_BUCKET, REPORT_TEMPLATE_BUCKET
-- ---------------------------------------------------------------------------

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
    (
        'answer-sheets', 'answer-sheets', false,
        10485760,  -- 10 MB: a phone photo of one sheet, not a scan batch
        array['image/jpeg', 'image/png', 'image/webp', 'application/pdf']
    ),
    (
        'certificates', 'certificates', false,
        5242880,   -- 5 MB
        array['application/pdf']
    ),
    (
        'report-templates', 'report-templates', false,
        26214400,  -- 25 MB: the EQWART workbook is macro-enabled and large
        array[
            'application/vnd.ms-excel.sheet.macroEnabled.12',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-excel'
        ]
    )
on conflict (id) do update
    set public             = excluded.public,
        file_size_limit    = excluded.file_size_limit,
        allowed_mime_types = excluded.allowed_mime_types;

-- ---------------------------------------------------------------------------
-- No policy is granted on storage.objects to `authenticated`.
--
-- That is deliberate, and it is what keeps these buckets closed: the API holds
-- the service-role key, which bypasses storage policies, so uploads and signed
-- URLs work from the server while a user token can neither list a bucket nor
-- fetch an object by guessing its path.
--
-- If a future screen ever needs to read a bucket directly with a user token,
-- add a policy here that re-states the same rule as the row it belongs to —
-- do not make a bucket public.
-- ---------------------------------------------------------------------------
