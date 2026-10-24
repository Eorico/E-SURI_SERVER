-- ---------------------------------------------------------------------------
-- E-SURI / IMECAR — reference data
--
-- Run after 0003_storage.sql.
--
-- The school and its eight Grade 11 learning areas, exactly as the supplied
-- EQWART workbook names them: `sheet_name` must match the worksheet tab, or the
-- exporter cannot find the sheet to write into.
--
-- Safe to re-run — every statement is idempotent. No staff accounts are created
-- here: a profile must correspond to a real Supabase auth user, so the first
-- administrator is made with scripts/bootstrap_admin.py instead.
-- ---------------------------------------------------------------------------

-- Keep the school year in one place; change it here when the cycle rolls over.
do $$
declare
    v_year   text := '2026-2027';
    v_school uuid;
begin

    insert into public.schools (name, division, cluster, school_year)
    values ('Paliparan III Senior High School', 'Division of Dasmariñas City', 'Cluster VIII', v_year)
    on conflict do nothing;

    select id into v_school
    from public.schools
    where name = 'Paliparan III Senior High School' and school_year = v_year;

    -- ---------------------------------------------------------------------
    -- Learning areas. `code` is ours; `sheet_name` is the workbook's.
    -- ---------------------------------------------------------------------
    insert into public.learning_areas (school_id, code, name, sheet_name, grade_level)
    values
        (v_school, 'GENSCI',  'General Science',                                'Gen Science',      11),
        (v_school, 'GENMATH', 'General Mathematics',                            'Gen Math',         11),
        (v_school, 'CL',      'Contemporary Literature',                        'CL',               11),
        (v_school, 'SSTP',    'Elective — Social Science',                      'SSTP',             11),
        (v_school, 'PKLP',    'Pag-aaral ng Kasaysayan ng Lipunang Pilipino',   'PKLP',             11),
        (v_school, 'LCS',     'Life and Career Skills',                         'LCS',              11),
        (v_school, 'ECOMM',   'Effective Communication',                        'E.Comm',           11),
        (v_school, 'MKOM',    'Mabisang Komunikasyon',                          'M. Kommunikasyon', 11)
    on conflict (school_id, code) do update
        set name       = excluded.name,
            sheet_name = excluded.sheet_name;

    -- ---------------------------------------------------------------------
    -- Sections.
    --
    -- `position` is the workbook column: section 1 is column B, and the sheet
    -- runs to column V, so a school may carry at most 21. Core areas use all
    -- 18 academic/TechPro sections; electives use a handful.
    -- ---------------------------------------------------------------------
    insert into public.sections (school_id, name, grade_level, school_year, position)
    values
        (v_school, 'ACAD ASSH 11-1',   11, v_year,  1),
        (v_school, 'ACAD ASSH 11-2',   11, v_year,  2),
        (v_school, 'ACAD ASSH 11-3',   11, v_year,  3),
        (v_school, 'ACAD STEM 11-1',   11, v_year,  4),
        (v_school, 'ACAD STEM 11-2',   11, v_year,  5),
        (v_school, 'ACAD STEM 11-3',   11, v_year,  6),
        (v_school, 'ACAD ABM 11-1',    11, v_year,  7),
        (v_school, 'ACAD ABM 11-2',    11, v_year,  8),
        (v_school, 'ACAD HUMSS 11-1',  11, v_year,  9),
        (v_school, 'ACAD HUMSS 11-2',  11, v_year, 10),
        (v_school, 'ACAD GAS 11-1',    11, v_year, 11),
        (v_school, 'ACAD GAS 11-2',    11, v_year, 12),
        (v_school, 'TechPro AWH 11-1', 11, v_year, 13),
        (v_school, 'TechPro AWH 11-2', 11, v_year, 14),
        (v_school, 'TechPro CRT 11-1', 11, v_year, 15),
        (v_school, 'TechPro CRT 11-2', 11, v_year, 16),
        (v_school, 'TechPro EIM 11-1', 11, v_year, 17),
        (v_school, 'TechPro SMAW 11-1',11, v_year, 18)
    on conflict (school_id, name, school_year) do update
        set position = excluded.position;

end $$;

-- The workbook cannot hold more than 21 sections per learning area (B–V), so
-- refuse the 22nd here rather than produce a report that silently drops it.
alter table public.sections drop constraint if exists ck_sections_position_range;
alter table public.sections
    add constraint ck_sections_position_range check (position between 1 and 21);
