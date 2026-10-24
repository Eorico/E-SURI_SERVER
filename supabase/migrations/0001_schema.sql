-- ---------------------------------------------------------------------------
-- E-SURI / IMECAR — schema
--
-- GENERATED from api/models/tables.py by scripts/generate_schema_sql.py.
-- Do not edit by hand: change the models and regenerate, or the API and the
-- database will disagree.
--
-- Run first, before 0002_rls.sql.
-- ---------------------------------------------------------------------------

create extension if not exists "pgcrypto";


CREATE TABLE IF NOT EXISTS schools (
	name VARCHAR(160) NOT NULL, 
	division VARCHAR(120), 
	cluster VARCHAR(120), 
	school_year VARCHAR(16) NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_schools PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS learning_areas (
	school_id UUID NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	sheet_name VARCHAR(64) NOT NULL, 
	grade_level INTEGER NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_learning_areas PRIMARY KEY (id), 
	CONSTRAINT uq_learning_areas_school_code UNIQUE (school_id, code), 
	CONSTRAINT fk_learning_areas_school_id_schools FOREIGN KEY(school_id) REFERENCES schools (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sections (
	school_id UUID NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	grade_level INTEGER NOT NULL, 
	school_year VARCHAR(16) NOT NULL, 
	position INTEGER NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_sections PRIMARY KEY (id), 
	CONSTRAINT uq_sections_school_name_year UNIQUE (school_id, name, school_year), 
	CONSTRAINT fk_sections_school_id_schools FOREIGN KEY(school_id) REFERENCES schools (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS students (
	school_id UUID NOT NULL, 
	student_no VARCHAR(32) NOT NULL, 
	full_name VARCHAR(160) NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_students PRIMARY KEY (id), 
	CONSTRAINT uq_students_school_student_no UNIQUE (school_id, student_no), 
	CONSTRAINT fk_students_school_id_schools FOREIGN KEY(school_id) REFERENCES schools (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS competencies (
	learning_area_id UUID NOT NULL, 
	code VARCHAR(48) NOT NULL, 
	description TEXT NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_competencies PRIMARY KEY (id), 
	CONSTRAINT uq_competencies_area_code UNIQUE (learning_area_id, code), 
	CONSTRAINT fk_competencies_learning_area_id_learning_areas FOREIGN KEY(learning_area_id) REFERENCES learning_areas (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS enrollments (
	student_id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_enrollments PRIMARY KEY (id), 
	CONSTRAINT uq_enrollments_student_section UNIQUE (student_id, section_id), 
	CONSTRAINT fk_enrollments_student_id_students FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE CASCADE, 
	CONSTRAINT fk_enrollments_section_id_sections FOREIGN KEY(section_id) REFERENCES sections (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS eqwart_reports (
	learning_area_id UUID NOT NULL, 
	assessment_type VARCHAR(40) NOT NULL, 
	school_year VARCHAR(16) NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	key_teacher_name VARCHAR(160), 
	reviewed_by_name VARCHAR(160), 
	approved_by_name VARCHAR(160), 
	intervention_activities JSON, 
	intervention_other TEXT, 
	intervention_mode VARCHAR(40), 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_eqwart_reports PRIMARY KEY (id), 
	CONSTRAINT uq_eqwart_area_type_year UNIQUE (learning_area_id, assessment_type, school_year), 
	CONSTRAINT fk_eqwart_reports_learning_area_id_learning_areas FOREIGN KEY(learning_area_id) REFERENCES learning_areas (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profiles (
	role VARCHAR(40) NOT NULL, 
	full_name VARCHAR(160), 
	username VARCHAR(64), 
	email VARCHAR(254), 
	phone VARCHAR(32), 
	is_active BOOLEAN NOT NULL, 
	school_id UUID, 
	learning_area_id UUID, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_profiles PRIMARY KEY (id), 
	CONSTRAINT uq_profiles_username UNIQUE (username), 
	CONSTRAINT uq_profiles_email UNIQUE (email), 
	CONSTRAINT fk_profiles_school_id_schools FOREIGN KEY(school_id) REFERENCES schools (id) ON DELETE SET NULL, 
	CONSTRAINT fk_profiles_learning_area_id_learning_areas FOREIGN KEY(learning_area_id) REFERENCES learning_areas (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
	actor_id UUID, 
	action VARCHAR(64) NOT NULL, 
	entity_type VARCHAR(64) NOT NULL, 
	entity_id UUID, 
	metadata_json JSON, 
	ip_address VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	CONSTRAINT pk_audit_logs PRIMARY KEY (id), 
	CONSTRAINT fk_audit_logs_actor_id_profiles FOREIGN KEY(actor_id) REFERENCES profiles (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_created ON audit_logs (actor_id, created_at);

CREATE TABLE IF NOT EXISTS classes (
	section_id UUID NOT NULL, 
	learning_area_id UUID NOT NULL, 
	teacher_id UUID NOT NULL, 
	class_code VARCHAR(32) NOT NULL, 
	school_year VARCHAR(16) NOT NULL, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_classes PRIMARY KEY (id), 
	CONSTRAINT uq_classes_section_area_year UNIQUE (section_id, learning_area_id, school_year), 
	CONSTRAINT fk_classes_section_id_sections FOREIGN KEY(section_id) REFERENCES sections (id) ON DELETE CASCADE, 
	CONSTRAINT fk_classes_learning_area_id_learning_areas FOREIGN KEY(learning_area_id) REFERENCES learning_areas (id) ON DELETE CASCADE, 
	CONSTRAINT fk_classes_teacher_id_profiles FOREIGN KEY(teacher_id) REFERENCES profiles (id) ON DELETE RESTRICT, 
	CONSTRAINT uq_classes_class_code UNIQUE (class_code)
);

CREATE TABLE IF NOT EXISTS least_mastered (
	report_id UUID NOT NULL, 
	position INTEGER NOT NULL, 
	rank_value INTEGER, 
	item_no INTEGER, 
	competency_id UUID, 
	competency_text TEXT, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_least_mastered PRIMARY KEY (id), 
	CONSTRAINT uq_least_mastered_report_position UNIQUE (report_id, position), 
	CONSTRAINT ck_least_mastered_position_in_range CHECK (position BETWEEN 1 AND 5), 
	CONSTRAINT fk_least_mastered_report_id_eqwart_reports FOREIGN KEY(report_id) REFERENCES eqwart_reports (id) ON DELETE CASCADE, 
	CONSTRAINT fk_least_mastered_competency_id_competencies FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS report_templates (
	name VARCHAR(160) NOT NULL, 
	original_filename VARCHAR(255) NOT NULL, 
	storage_path VARCHAR(512) NOT NULL, 
	checksum VARCHAR(64) NOT NULL, 
	school_year VARCHAR(16) NOT NULL, 
	grade_level INTEGER NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	mapping JSON, 
	mapping_source VARCHAR(40) NOT NULL, 
	mapping_confidence FLOAT NOT NULL, 
	mapping_notes JSON, 
	dry_run_passed BOOLEAN NOT NULL, 
	uploaded_by UUID, 
	activated_at TIMESTAMP WITH TIME ZONE, 
	activated_by UUID, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_report_templates PRIMARY KEY (id), 
	CONSTRAINT uq_templates_year_grade_status UNIQUE (school_year, grade_level, status), 
	CONSTRAINT fk_report_templates_uploaded_by_profiles FOREIGN KEY(uploaded_by) REFERENCES profiles (id) ON DELETE SET NULL, 
	CONSTRAINT fk_report_templates_activated_by_profiles FOREIGN KEY(activated_by) REFERENCES profiles (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_report_templates_status ON report_templates (status);

CREATE TABLE IF NOT EXISTS assessments (
	title VARCHAR(160) NOT NULL, 
	type VARCHAR(40) NOT NULL, 
	class_id UUID NOT NULL, 
	item_count INTEGER NOT NULL, 
	test_code VARCHAR(24) NOT NULL, 
	options VARCHAR(10) NOT NULL, 
	date DATE, 
	status VARCHAR(40) NOT NULL, 
	key_locked_at TIMESTAMP WITH TIME ZONE, 
	created_by UUID, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_assessments PRIMARY KEY (id), 
	CONSTRAINT uq_assessments_class_type UNIQUE (class_id, type), 
	CONSTRAINT ck_assessments_item_count_positive CHECK (item_count > 0), 
	CONSTRAINT fk_assessments_class_id_classes FOREIGN KEY(class_id) REFERENCES classes (id) ON DELETE CASCADE, 
	CONSTRAINT uq_assessments_test_code UNIQUE (test_code), 
	CONSTRAINT fk_assessments_created_by_profiles FOREIGN KEY(created_by) REFERENCES profiles (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_assessments_class_status ON assessments (class_id, status);

CREATE TABLE IF NOT EXISTS certificates (
	student_id UUID NOT NULL, 
	class_id UUID NOT NULL, 
	type VARCHAR(40) NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	issued_by UUID, 
	issued_at TIMESTAMP WITH TIME ZONE, 
	pdf_path VARCHAR(512), 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_certificates PRIMARY KEY (id), 
	CONSTRAINT uq_certificates_student_class UNIQUE (student_id, class_id), 
	CONSTRAINT fk_certificates_student_id_students FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE CASCADE, 
	CONSTRAINT fk_certificates_class_id_classes FOREIGN KEY(class_id) REFERENCES classes (id) ON DELETE CASCADE, 
	CONSTRAINT fk_certificates_issued_by_profiles FOREIGN KEY(issued_by) REFERENCES profiles (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS answer_keys (
	assessment_id UUID NOT NULL, 
	item_no INTEGER NOT NULL, 
	correct_option VARCHAR(1) NOT NULL, 
	points INTEGER NOT NULL, 
	competency_id UUID, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	CONSTRAINT pk_answer_keys PRIMARY KEY (id), 
	CONSTRAINT uq_answer_keys_assessment_item UNIQUE (assessment_id, item_no), 
	CONSTRAINT ck_answer_keys_item_no_positive CHECK (item_no >= 1), 
	CONSTRAINT fk_answer_keys_assessment_id_assessments FOREIGN KEY(assessment_id) REFERENCES assessments (id) ON DELETE CASCADE, 
	CONSTRAINT fk_answer_keys_competency_id_competencies FOREIGN KEY(competency_id) REFERENCES competencies (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS answer_sheets (
	assessment_id UUID NOT NULL, 
	student_id UUID NOT NULL, 
	sheet_code VARCHAR(32) NOT NULL, 
	pdf_path VARCHAR(512), 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_answer_sheets PRIMARY KEY (id), 
	CONSTRAINT uq_answer_sheets_assessment_student UNIQUE (assessment_id, student_id), 
	CONSTRAINT fk_answer_sheets_assessment_id_assessments FOREIGN KEY(assessment_id) REFERENCES assessments (id) ON DELETE CASCADE, 
	CONSTRAINT fk_answer_sheets_student_id_students FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS item_analysis (
	assessment_id UUID NOT NULL, 
	item_no INTEGER NOT NULL, 
	correct_count INTEGER NOT NULL, 
	attempted_count INTEGER NOT NULL, 
	percent_correct FLOAT, 
	rank INTEGER, 
	discrimination_index FLOAT, 
	mastery_level VARCHAR(40), 
	instructional_assistance VARCHAR(40), 
	distribution JSON, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_item_analysis PRIMARY KEY (id), 
	CONSTRAINT uq_item_analysis_assessment_item UNIQUE (assessment_id, item_no), 
	CONSTRAINT fk_item_analysis_assessment_id_assessments FOREIGN KEY(assessment_id) REFERENCES assessments (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS submissions (
	assessment_id UUID NOT NULL, 
	student_id UUID NOT NULL, 
	image_path VARCHAR(512), 
	name_strip_path VARCHAR(512), 
	score INTEGER, 
	status VARCHAR(40) NOT NULL, 
	flagged_count INTEGER NOT NULL, 
	scanned_at TIMESTAMP WITH TIME ZONE, 
	scanned_by UUID, 
	approved_at TIMESTAMP WITH TIME ZONE, 
	approved_by UUID, 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_submissions PRIMARY KEY (id), 
	CONSTRAINT uq_submissions_assessment_student UNIQUE (assessment_id, student_id), 
	CONSTRAINT fk_submissions_assessment_id_assessments FOREIGN KEY(assessment_id) REFERENCES assessments (id) ON DELETE CASCADE, 
	CONSTRAINT fk_submissions_student_id_students FOREIGN KEY(student_id) REFERENCES students (id) ON DELETE CASCADE, 
	CONSTRAINT fk_submissions_scanned_by_profiles FOREIGN KEY(scanned_by) REFERENCES profiles (id) ON DELETE SET NULL, 
	CONSTRAINT fk_submissions_approved_by_profiles FOREIGN KEY(approved_by) REFERENCES profiles (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_submissions_assessment_status ON submissions (assessment_id, status);

CREATE TABLE IF NOT EXISTS responses (
	submission_id UUID NOT NULL, 
	item_no INTEGER NOT NULL, 
	marked_option VARCHAR(10), 
	is_correct BOOLEAN NOT NULL, 
	flag VARCHAR(40) NOT NULL, 
	detected_option VARCHAR(10), 
	id UUID NOT NULL DEFAULT gen_random_uuid(), 
	CONSTRAINT pk_responses PRIMARY KEY (id), 
	CONSTRAINT uq_responses_submission_item UNIQUE (submission_id, item_no), 
	CONSTRAINT fk_responses_submission_id_submissions FOREIGN KEY(submission_id) REFERENCES submissions (id) ON DELETE CASCADE
);


-- Value sets, enforced in the database as well as in the DTOs.
alter table public.profiles drop constraint if exists ck_profiles_role;
alter table public.profiles
    add constraint ck_profiles_role check (role in ('subject_teacher', 'subject_coordinator', 'administrator'));
alter table public.assessments drop constraint if exists ck_assessments_type;
alter table public.assessments
    add constraint ck_assessments_type check (type in ('pre', 'st1', 'st2', 'term', 'post'));
alter table public.assessments drop constraint if exists ck_assessments_status;
alter table public.assessments
    add constraint ck_assessments_status check (status in ('draft', 'scheduled', 'in_progress', 'completed'));
alter table public.submissions drop constraint if exists ck_submissions_status;
alter table public.submissions
    add constraint ck_submissions_status check (status in ('missing', 'review', 'approved'));
alter table public.responses drop constraint if exists ck_responses_flag;
alter table public.responses
    add constraint ck_responses_flag check (flag in ('none', 'blank', 'multiple'));
alter table public.enrollments drop constraint if exists ck_enrollments_status;
alter table public.enrollments
    add constraint ck_enrollments_status check (status in ('active', 'transferred', 'dropped'));
alter table public.certificates drop constraint if exists ck_certificates_type;
alter table public.certificates
    add constraint ck_certificates_type check (type in ('completion', 'participation'));
alter table public.certificates drop constraint if exists ck_certificates_status;
alter table public.certificates
    add constraint ck_certificates_status check (status in ('pending', 'released', 'revoked'));
alter table public.report_templates drop constraint if exists ck_report_templates_status;
alter table public.report_templates
    add constraint ck_report_templates_status check (status in ('draft', 'active', 'archived', 'rejected'));
alter table public.report_templates drop constraint if exists ck_report_templates_mapping_source;
alter table public.report_templates
    add constraint ck_report_templates_mapping_source check (mapping_source in ('ai', 'heuristic', 'manual'));
alter table public.eqwart_reports drop constraint if exists ck_eqwart_reports_status;
alter table public.eqwart_reports
    add constraint ck_eqwart_reports_status check (status in ('draft', 'submitted', 'reviewed', 'approved'));
alter table public.eqwart_reports drop constraint if exists ck_eqwart_reports_assessment_type;
alter table public.eqwart_reports
    add constraint ck_eqwart_reports_assessment_type check (assessment_type in ('pre', 'st1', 'st2', 'term', 'post'));
alter table public.eqwart_reports drop constraint if exists ck_eqwart_reports_intervention_mode;
alter table public.eqwart_reports
    add constraint ck_eqwart_reports_intervention_mode check (intervention_mode in ('Add-on', 'Pull-out', 'Built-in'));
alter table public.item_analysis drop constraint if exists ck_item_analysis_mastery_level;
alter table public.item_analysis
    add constraint ck_item_analysis_mastery_level check (mastery_level in ('Not Proficient', 'Low Proficient', 'Nearly Proficient', 'Proficient', 'Highly Proficient'));
alter table public.item_analysis drop constraint if exists ck_item_analysis_instructional_assistance;
alter table public.item_analysis
    add constraint ck_item_analysis_instructional_assistance check (instructional_assistance in ('Reteach', 'Remediation', 'Intervention', 'Enhancement', 'Advancement'));


-- ---------------------------------------------------------------------------
-- Ties a profile to the Supabase auth user it belongs to.
--
-- Added here rather than in the models because `auth.users` is Supabase's own
-- table. Deleting the auth user now removes the profile with it, so no row is
-- left behind that nobody can sign in as.
-- ---------------------------------------------------------------------------
alter table public.profiles
    drop constraint if exists fk_profiles_id_auth_users;

alter table public.profiles
    add constraint fk_profiles_id_auth_users
    foreign key (id) references auth.users (id) on delete cascade;

-- A school runs one assessment cycle at a time; these are the lookups every
-- screen makes, so they are worth the write cost.
create index if not exists ix_classes_teacher on public.classes (teacher_id);
create index if not exists ix_classes_area on public.classes (learning_area_id);
create index if not exists ix_enrollments_class on public.enrollments (class_id);
create index if not exists ix_submissions_student on public.submissions (student_id);
create index if not exists ix_responses_submission on public.responses (submission_id);
