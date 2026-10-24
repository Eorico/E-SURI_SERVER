-- ---------------------------------------------------------------------------
-- E-SURI / IMECAR — row-level security
--
-- Run after 0001_schema.sql.
--
-- Why this exists even though the API already checks roles
-- --------------------------------------------------------
-- The API is the only client with a Supabase key, and it enforces roles in
-- `api/core/security.py`. RLS is the second lock: if a token ever leaks, or a
-- future screen talks to PostgREST directly, the database still refuses to
-- hand a teacher another teacher's learners.
--
-- The backend's own connection uses the service-role key, which bypasses every
-- policy below by design — that is what lets it assemble a coordinator's
-- cross-class report. These policies constrain anyone holding a *user* token.
--
-- The rule, in one line: a teacher sees their own classes, a coordinator sees
-- their learning area, an administrator sees the school.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Helpers
--
-- Each reads the caller's profile once. They are STABLE so Postgres evaluates
-- them once per statement rather than once per row, which matters: these sit
-- in the USING clause of a table with one row per learner per item.
--
-- SECURITY DEFINER is required — the policies on `profiles` would otherwise
-- recurse when a policy on another table tries to read the caller's role.
-- `search_path` is pinned for the same reason it always is on a definer
-- function: so the body cannot be redirected to a caller-supplied schema.
-- ---------------------------------------------------------------------------
create or replace function public.current_role_name()
returns text
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select role from public.profiles where id = auth.uid() and is_active
$$;

create or replace function public.current_school_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select school_id from public.profiles where id = auth.uid() and is_active
$$;

create or replace function public.current_learning_area_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select learning_area_id from public.profiles where id = auth.uid() and is_active
$$;

create or replace function public.is_administrator()
returns boolean
language sql
stable
as $$
    select public.current_role_name() = 'administrator'
$$;

-- True when the caller may act on this class: the teacher who owns it, the
-- coordinator for its learning area, or an administrator.
create or replace function public.can_read_class(target_class_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1
        from public.classes c
        where c.id = target_class_id
          and (
                public.is_administrator()
             or c.teacher_id = auth.uid()
             or (
                    public.current_role_name() = 'subject_coordinator'
                and c.learning_area_id = public.current_learning_area_id()
                )
          )
    )
$$;

-- A learner is visible when they sit in a section the caller teaches, or
-- coordinates, or administers.
create or replace function public.can_read_student(target_student_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select public.is_administrator() or exists (
        select 1
        from public.enrollments e
        join public.classes c on c.section_id = e.section_id
        where e.student_id = target_student_id
          and public.can_read_class(c.id)
    )
$$;

create or replace function public.can_read_assessment(target_assessment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1 from public.assessments a
        where a.id = target_assessment_id and public.can_read_class(a.class_id)
    )
$$;

create or replace function public.can_read_submission(target_submission_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1 from public.submissions s
        where s.id = target_submission_id and public.can_read_assessment(s.assessment_id)
    )
$$;

-- The answer key is the test itself, so a coordinator does not get it early:
-- the owning teacher and administrators only.
create or replace function public.can_read_answer_key(target_assessment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1
        from public.assessments a
        join public.classes c on c.id = a.class_id
        where a.id = target_assessment_id
          and (public.is_administrator() or c.teacher_id = auth.uid())
    )
$$;

-- A learning area is readable by its coordinator, by any teacher who holds a
-- class in it, and by administrators. EQWART is a learning-area document.
create or replace function public.can_read_learning_area(target_area_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select public.is_administrator()
        or target_area_id = public.current_learning_area_id()
        or exists (
            select 1 from public.classes c
            where c.learning_area_id = target_area_id and c.teacher_id = auth.uid()
        )
$$;

create or replace function public.can_read_report(target_report_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1 from public.eqwart_reports r
        where r.id = target_report_id
          and public.can_read_learning_area(r.learning_area_id)
    )
$$;

-- Whether a learning area belongs to the caller's school. Used by the
-- competency policy, which would otherwise re-enter the learning_areas policy
-- for every curriculum row on the screen.
create or replace function public.is_school_learning_area(target_area_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select exists (
        select 1 from public.learning_areas la
        where la.id = target_area_id and la.school_id = public.current_school_id()
    )
$$;

-- ---------------------------------------------------------------------------
-- Enable RLS everywhere.
--
-- A table with RLS on and no policy denies every row to a user token, which is
-- the safe default: a table added later is closed until someone opens it
-- deliberately.
-- ---------------------------------------------------------------------------
alter table public.schools            enable row level security;
alter table public.learning_areas     enable row level security;
alter table public.sections           enable row level security;
alter table public.students           enable row level security;
alter table public.competencies       enable row level security;
alter table public.enrollments        enable row level security;
alter table public.profiles           enable row level security;
alter table public.audit_logs         enable row level security;
alter table public.classes            enable row level security;
alter table public.assessments        enable row level security;
alter table public.answer_keys        enable row level security;
alter table public.answer_sheets      enable row level security;
alter table public.submissions        enable row level security;
alter table public.responses          enable row level security;
alter table public.item_analysis      enable row level security;
alter table public.eqwart_reports     enable row level security;
alter table public.least_mastered     enable row level security;
alter table public.certificates       enable row level security;
alter table public.report_templates   enable row level security;

-- ---------------------------------------------------------------------------
-- Reference data: readable by any signed-in member of the school.
--
-- Nothing here is about an individual learner — it is the school, its subjects,
-- its sections and the curriculum competencies. Writes stay with the API.
-- ---------------------------------------------------------------------------
drop policy if exists schools_read on public.schools;
create policy schools_read on public.schools
    for select to authenticated
    using (id = public.current_school_id());

drop policy if exists learning_areas_read on public.learning_areas;
create policy learning_areas_read on public.learning_areas
    for select to authenticated
    using (school_id = public.current_school_id());

drop policy if exists sections_read on public.sections;
create policy sections_read on public.sections
    for select to authenticated
    using (school_id = public.current_school_id());

drop policy if exists competencies_read on public.competencies;
create policy competencies_read on public.competencies
    for select to authenticated
    using (public.is_school_learning_area(learning_area_id));

-- ---------------------------------------------------------------------------
-- Profiles
--
-- Everyone reads their own row; that is what the app needs to render the right
-- workspace. An administrator reads the school's staff. Nobody changes a role
-- through PostgREST — role assignment is an API route, deliberately, so it is
-- audited and cannot be self-applied.
-- ---------------------------------------------------------------------------
drop policy if exists profiles_read_self on public.profiles;
create policy profiles_read_self on public.profiles
    for select to authenticated
    using (id = auth.uid());

drop policy if exists profiles_read_school on public.profiles;
create policy profiles_read_school on public.profiles
    for select to authenticated
    using (public.is_administrator() and school_id = public.current_school_id());

drop policy if exists profiles_update_self on public.profiles;
create policy profiles_update_self on public.profiles
    for update to authenticated
    using (id = auth.uid())
    with check (id = auth.uid() and role = public.current_role_name());

-- ---------------------------------------------------------------------------
-- Learners
--
-- A teacher may see a learner who is enrolled in a section they teach. This is
-- the policy that stops one teacher browsing another's roster.
-- ---------------------------------------------------------------------------
drop policy if exists students_read on public.students;
create policy students_read on public.students
    for select to authenticated
    using (school_id = public.current_school_id() and public.can_read_student(id));

drop policy if exists enrollments_read on public.enrollments;
create policy enrollments_read on public.enrollments
    for select to authenticated
    using (public.can_read_student(student_id));

-- ---------------------------------------------------------------------------
-- Classes and assessments
-- ---------------------------------------------------------------------------
drop policy if exists classes_read on public.classes;
create policy classes_read on public.classes
    for select to authenticated
    using (public.can_read_class(id));

drop policy if exists assessments_read on public.assessments;
create policy assessments_read on public.assessments
    for select to authenticated
    using (public.can_read_class(class_id));

drop policy if exists answer_keys_read on public.answer_keys;
create policy answer_keys_read on public.answer_keys
    for select to authenticated
    using (public.can_read_answer_key(assessment_id));

-- ---------------------------------------------------------------------------
-- Scans and scores
-- ---------------------------------------------------------------------------
drop policy if exists submissions_read on public.submissions;
create policy submissions_read on public.submissions
    for select to authenticated
    using (public.can_read_assessment(assessment_id));

drop policy if exists answer_sheets_read on public.answer_sheets;
create policy answer_sheets_read on public.answer_sheets
    for select to authenticated
    using (public.can_read_submission(submission_id));

drop policy if exists responses_read on public.responses;
create policy responses_read on public.responses
    for select to authenticated
    using (public.can_read_submission(submission_id));

drop policy if exists item_analysis_read on public.item_analysis;
create policy item_analysis_read on public.item_analysis
    for select to authenticated
    using (public.can_read_assessment(assessment_id));

-- ---------------------------------------------------------------------------
-- Reports and certificates
--
-- EQWART is a learning-area document, so it is the coordinator's view rather
-- than the individual teacher's.
-- ---------------------------------------------------------------------------
drop policy if exists eqwart_reports_read on public.eqwart_reports;
create policy eqwart_reports_read on public.eqwart_reports
    for select to authenticated
    using (public.can_read_learning_area(learning_area_id));

drop policy if exists least_mastered_read on public.least_mastered;
create policy least_mastered_read on public.least_mastered
    for select to authenticated
    using (public.can_read_report(report_id));

drop policy if exists certificates_read on public.certificates;
create policy certificates_read on public.certificates
    for select to authenticated
    using (public.can_read_class(class_id));

-- ---------------------------------------------------------------------------
-- Report templates and the audit trail: administrators only.
--
-- A template defines how every learner's result is written into the CSDO
-- workbook, so uploading one is an administrator action and reading one tells
-- you nothing a teacher needs.
-- ---------------------------------------------------------------------------
drop policy if exists report_templates_admin on public.report_templates;
create policy report_templates_admin on public.report_templates
    for select to authenticated
    using (public.is_administrator());

drop policy if exists audit_logs_admin on public.audit_logs;
create policy audit_logs_admin on public.audit_logs
    for select to authenticated
    using (public.is_administrator());

-- ---------------------------------------------------------------------------
-- No INSERT, UPDATE or DELETE policy is granted to `authenticated` anywhere
-- except a user's own profile. Every write goes through the API, which is what
-- makes the audit log complete and the rate limits meaningful. The service
-- role bypasses RLS, so the API is unaffected.
-- ---------------------------------------------------------------------------
