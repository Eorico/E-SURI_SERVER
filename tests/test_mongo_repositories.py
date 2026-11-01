"""The data layer, against an in-process MongoDB.

Every repository method the services call, against a real query planner. The
ones worth reading are the id coercion - a bug that fails silently, returning
nothing rather than raising - and the embedded arrays, where a uniqueness rule
no index can reach is enforced on the document instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from api.models import documents as D
from api.models.enums import (
    AssessmentStatus,
    AssessmentType,
    CertificateStatus,
    CertificateType,
    EnrollmentStatus,
    SubmissionStatus,
    UserRole,
)
from api.repositories.analysis_repository import (
    CertificateRepository,
    EqwartReportRepository,
    ItemAnalysisRepository,
    LeastMasteredRepository,
)
from api.repositories.assessment_repository import (
    AnswerKeyRepository,
    AssessmentRepository,
)
from api.repositories.audit_repository import AuditRepository
from api.repositories.base import Coercing, MongoRepository
from api.repositories.class_repository import ClassRepository
from api.repositories.profile_repository import (
    CoordinatorAssignmentRepository,
    ProfileRepository,
)
from api.repositories.school_repository import (
    LearningAreaRepository,
    SchoolRepository,
    SectionRepository,
)
from api.repositories.student_repository import StudentRepository
from api.repositories.submission_repository import (
    ResponseRepository,
    SubmissionRepository,
)


# --------------------------------------------------------------------------- #
# Identifiers                                                                  #
# --------------------------------------------------------------------------- #
class TestIdentifiers:
    """Ids are stored as strings and read back as UUIDs.

    The reason this gets its own class: a `uuid.UUID` left in a filter matches
    nothing and raises nothing. There is no error to notice, only a screen that
    is empty - so the coercion is tested directly rather than inferred from
    some other test passing.
    """

    def test_an_id_round_trips_as_a_uuid(self, db, school):
        found = SchoolRepository(db).get(school.id)
        assert isinstance(found.id, uuid.UUID)
        assert found.id == school.id

    def test_it_is_a_string_in_the_document(self, db, school):
        raw = db["schools"].find_one({})
        assert isinstance(raw["_id"], str)
        assert raw["_id"] == str(school.id)

    def test_a_uuid_in_a_filter_still_matches(self, db, klass, teacher):
        """The coercion under test. Without it this returns nothing."""
        assert ClassRepository(db).list(teacher_id=teacher.id) != []

    def test_a_uuid_inside_an_in_clause_still_matches(self, db, students):
        """`$in` is the case a per-field fix would have missed."""
        ids = [student.id for student in students]
        assert StudentRepository(db).existing_ids(ids) == set(ids)

    def test_the_proxy_coerces_whatever_it_is_given(self, db):
        """Every argument, at any depth - the proxy knows nothing about filters."""
        identifier = uuid.uuid4()
        collection = Coercing(db["anything"])
        collection.insert_one({"_id": identifier, "nested": {"ref": identifier}})

        raw = db["anything"].find_one({})
        assert raw["_id"] == str(identifier)
        assert raw["nested"]["ref"] == str(identifier)
        assert collection.find_one({"nested.ref": identifier}) is not None


# --------------------------------------------------------------------------- #
# Profiles and the role details that used to be two tables                     #
# --------------------------------------------------------------------------- #
class TestProfiles:
    def test_with_role_details_is_now_one_read(self, db, school):
        """The three-query path, collapsed by the document shape.

        `admin_type` and `learning_area_id` both read the role details. Held in
        separate collections that would be three reads on every authenticated
        request; embedded it is one primary-key read - and this asserts the
        count rather than trusting it.
        """
        from api.models.enums import AdminType

        profile = ProfileRepository(db).add(
            D.Profile(
                role=UserRole.administrator,
                last_name="Reyes",
                first_name="Ana",
                username="areyes",
                email="areyes@example.ph",
                school_id=school.id,
                administrator_profile=D.AdministratorProfile(
                    admin_type=AdminType.assessment_coordinator
                ),
            )
        )

        reads: list[dict] = []
        original = db["users"].find_one

        def counting(*args, **kwargs):
            reads.append(args[0] if args else {})
            return original(*args, **kwargs)

        db["users"].find_one = counting
        try:
            loaded = ProfileRepository(db).with_role_details(profile.id)
        finally:
            db["users"].find_one = original

        assert len(reads) == 1, f"expected one read, issued {len(reads)}"
        assert loaded.admin_type is AdminType.assessment_coordinator
        assert loaded.department_id is None

    def test_names_are_assembled_from_the_parts(self, db, teacher):
        loaded = ProfileRepository(db).get(teacher.id)
        assert loaded.full_name == "Maria S. Dela Cruz"
        assert loaded.formal_name == "Dela Cruz, Maria S."

    def test_lookup_by_email_and_username_is_case_folded(self, db, teacher):
        profiles = ProfileRepository(db)
        assert profiles.by_email("MDELACRUZ@EXAMPLE.PH") is not None
        assert profiles.by_username("MDELACRUZ") is not None

    def test_teachers_in_a_learning_area_come_from_their_classes(
        self, db, klass, teacher, learning_area
    ):
        """A teacher's subjects come from what they teach, not from a field."""
        found = ProfileRepository(db).teachers_in_learning_area(learning_area.id)
        assert [profile.id for profile in found] == [teacher.id]

    def test_an_area_with_no_classes_has_no_teachers(self, db, klass):
        assert ProfileRepository(db).teachers_in_learning_area(uuid.uuid4()) == []


class TestCoordinatorAssignment:
    """One area per coordinator, and never on anybody else."""

    @pytest.fixture
    def coordinator(self, db, school) -> D.Profile:
        return ProfileRepository(db).add(
            D.Profile(
                role=UserRole.subject_coordinator,
                last_name="Santos",
                first_name="Jose",
                username="jsantos",
                email="jsantos@example.ph",
                school_id=school.id,
            )
        )

    def test_assigning_then_reading_it_back(self, db, coordinator, learning_area):
        assignments = CoordinatorAssignmentRepository(db)
        assignments.assign(coordinator, learning_area.id)

        loaded = ProfileRepository(db).get(coordinator.id)
        assert loaded.learning_area_id == learning_area.id
        assert assignments.for_user(coordinator.id) is not None

    def test_reassigning_moves_it_rather_than_adding_one(
        self, db, coordinator, learning_area, school
    ):
        second = LearningAreaRepository(db).add(
            D.LearningArea(
                school_id=school.id, code="GENMATH", name="General Math", sheet_name="Gen Math"
            )
        )
        assignments = CoordinatorAssignmentRepository(db)
        assignments.assign(coordinator, learning_area.id)
        assignments.assign(coordinator, second.id)

        assert ProfileRepository(db).get(coordinator.id).learning_area_id == second.id

    def test_clearing_it(self, db, coordinator, learning_area):
        assignments = CoordinatorAssignmentRepository(db)
        assignments.assign(coordinator, learning_area.id)
        assignments.clear_for(coordinator)
        assert ProfileRepository(db).get(coordinator.id).learning_area_id is None

    def test_a_teacher_cannot_be_given_a_learning_area(
        self, db, teacher, learning_area
    ):
        """The rule the composite foreign key used to enforce."""
        with pytest.raises(ValueError):
            CoordinatorAssignmentRepository(db).assign(teacher, learning_area.id)

    def test_the_coordinator_for_an_area_is_findable(
        self, db, coordinator, learning_area
    ):
        CoordinatorAssignmentRepository(db).assign(coordinator, learning_area.id)
        found = ProfileRepository(db).coordinator_for(learning_area.id)
        assert found is not None and found.id == coordinator.id


# --------------------------------------------------------------------------- #
# Classes and hydration                                                        #
# --------------------------------------------------------------------------- #
class TestClassHydration:
    """Filling a class's section, learning area and teacher in one batch."""

    def test_get_full_fills_every_slot(self, db, klass, section, learning_area, teacher):
        loaded = ClassRepository(db).get_full(klass.id)
        assert loaded.section.id == section.id
        assert loaded.section.name == section.name
        assert loaded.learning_area.id == learning_area.id
        assert loaded.teacher.id == teacher.id

    def test_hydrating_a_list_does_not_scale_with_its_length(
        self, db, school, section, learning_area, teacher
    ):
        """Three queries for forty classes, not three per class.

        Resolving each class's relations one at a time would pass every other
        test in this file while being the thing worth catching, so the query
        count is asserted rather than the result.
        """
        areas = LearningAreaRepository(db)
        classes = ClassRepository(db)
        for index in range(40):
            area = areas.add(
                D.LearningArea(
                    school_id=school.id,
                    code=f"SUBJ{index}",
                    name=f"Subject {index}",
                    sheet_name=f"S{index}",
                )
            )
            classes.add(
                D.Class(
                    section_id=section.id,
                    learning_area_id=area.id,
                    teacher_id=teacher.id,
                    class_code=f"CODE-{index}",
                    school_year="2025-2026",
                )
            )

        queries: list[str] = []
        for name in ("sections", "learning_areas", "users"):
            original = db[name].find
            db[name].find = (
                lambda *a, _name=name, _original=original, **k: (
                    queries.append(_name) or _original(*a, **k)
                )
            )

        loaded = ClassRepository(db).by_teacher(teacher.id)

        assert len(loaded) == 40
        assert len(queries) == 3, f"expected 3 lookups, issued {len(queries)}: {queries}"
        assert all(record.section is not None for record in loaded)

    def test_a_dangling_reference_reads_as_missing(self, db, klass, section):
        """What `ON DELETE SET NULL` left behind must not hide the class."""
        SectionRepository(db).delete(section)
        loaded = ClassRepository(db).get_full(klass.id)
        assert loaded is not None
        assert loaded.section is None

    def test_the_roster_is_the_sections_active_learners(
        self, db, klass, students, enrolled
    ):
        roster = ClassRepository(db).roster(klass.id)
        assert [student.full_name for student in roster] == [
            "Abad, Juan",
            "Bautista, Rosa",
            "Castro, Mark",
        ]

    def test_a_withdrawn_learner_leaves_the_roster(
        self, db, klass, students, enrolled
    ):
        from api.repositories.student_repository import EnrollmentRepository

        EnrollmentRepository(db).update(enrolled[0], status=EnrollmentStatus.dropped)
        roster = ClassRepository(db).roster(klass.id)
        assert students[0].id not in {student.id for student in roster}

    def test_by_code_finds_one(self, db, klass):
        assert ClassRepository(db).by_code("ASSH111-GENSCI").id == klass.id


# --------------------------------------------------------------------------- #
# The embedded arrays                                                          #
# --------------------------------------------------------------------------- #
class TestAnswerKeys:
    """Was `answer_keys`, a table. Now an array inside its assessment."""

    def test_the_whole_key_reads_back_in_item_order(self, db, assessment):
        keys = AnswerKeyRepository(db).for_assessment(assessment.id)
        assert len(keys) == 60
        assert [key.item_no for key in keys] == list(range(1, 61))

    def test_as_map_is_what_the_scorer_consumes(self, db, assessment):
        mapping = AnswerKeyRepository(db).as_map(assessment.id)
        assert mapping[1] == "B"
        assert len(mapping) == 60

    def test_one_item_can_be_read_on_its_own(self, db, assessment):
        key = AnswerKeyRepository(db).item(assessment.id, 7)
        assert key is not None and key.item_no == 7

    def test_updating_one_item_leaves_the_rest(self, db, assessment):
        keys = AnswerKeyRepository(db)
        keys.update(keys.item(assessment.id, 7), correct_option="E")

        after = keys.as_map(assessment.id)
        assert after[7] == "E"
        assert len(after) == 60

    def test_adding_an_item(self, db, klass):
        assessments = AssessmentRepository(db)
        bare = assessments.add(
            D.Assessment(
                title="Quiz",
                type=AssessmentType.st1,
                class_id=klass.id,
                item_count=30,
                test_code="Q-1",
            )
        )
        keys = AnswerKeyRepository(db)
        keys.add(D.AnswerKey(assessment_id=bare.id, item_no=1, correct_option="C"))
        assert keys.as_map(bare.id) == {1: "C"}

    def test_clearing_then_replacing_the_key(self, db, assessment):
        keys = AnswerKeyRepository(db)
        keys.delete_where(assessment_id=assessment.id)
        assert keys.for_assessment(assessment.id) == []

        keys.add_all(
            [
                D.AnswerKey(assessment_id=assessment.id, item_no=item, correct_option="A")
                for item in range(1, 31)
            ]
        )
        assert len(keys.for_assessment(assessment.id)) == 30

    def test_a_duplicate_item_number_is_refused(self, db, assessment):
        """Was `uq_answer_keys_assessment_item`. No index can reach into an array.

        It is not cosmetic: a duplicated key would make the right answer depend
        on which copy was read first.
        """
        keys = AnswerKeyRepository(db)
        with pytest.raises(Exception):
            keys.add(
                D.AnswerKey(assessment_id=assessment.id, item_no=7, correct_option="A")
            )


class TestResponses:
    """Was `responses`, 60 rows per learner. Now an array inside the submission."""

    @pytest.fixture
    def submission(self, db, assessment, students) -> D.Submission:
        return SubmissionRepository(db).add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[0].id,
                status=SubmissionStatus.approved,
                score=40,
            )
        )

    def test_a_rescan_replaces_the_previous_reading(self, db, assessment, submission):
        from tests.conftest import make_responses

        key = AnswerKeyRepository(db).as_map(assessment.id)
        responses = ResponseRepository(db)

        responses.replace_for_submission(submission.id, make_responses(key, 40))
        assert len(responses.for_submission(submission.id)) == 60

        responses.replace_for_submission(submission.id, make_responses(key, 55))
        stored = responses.for_submission(submission.id)
        assert len(stored) == 60, "a rescan must supersede, not accumulate"
        assert sum(1 for item in stored if item.is_correct) == 55

    def test_answers_arrive_with_the_submission(self, db, assessment, submission):
        """The read that used to need a second query for sixty rows."""
        from tests.conftest import make_responses

        key = AnswerKeyRepository(db).as_map(assessment.id)
        ResponseRepository(db).replace_for_submission(
            submission.id, make_responses(key, 40)
        )

        loaded = SubmissionRepository(db).with_responses(submission.id)
        assert len(loaded.responses) == 60
        assert loaded.student is not None, "the learner is filled in too"

    def test_approved_papers_come_back_with_their_answers(
        self, db, assessment, submission
    ):
        from tests.conftest import make_responses

        key = AnswerKeyRepository(db).as_map(assessment.id)
        ResponseRepository(db).replace_for_submission(
            submission.id, make_responses(key, 40)
        )

        papers = SubmissionRepository(db).approved_with_responses(assessment.id)
        assert len(papers) == 1
        assert len(papers[0].responses) == 60

    def test_responses_stay_in_item_order(self, db, submission):
        responses = ResponseRepository(db)
        responses.add_all(
            [
                D.Response(submission_id=submission.id, item_no=item)
                for item in (5, 1, 3)
            ]
        )
        assert [item.item_no for item in responses.for_submission(submission.id)] == [
            1,
            3,
            5,
        ]

    def test_a_duplicate_item_number_is_refused(self, db, submission):
        """Was `uq_responses_submission_item`. A second answer for one item
        would be counted twice in the score."""
        responses = ResponseRepository(db)
        responses.add(D.Response(submission_id=submission.id, item_no=1))
        with pytest.raises(Exception):
            responses.add(D.Response(submission_id=submission.id, item_no=1))


class TestLeastMastered:
    """Was `least_mastered`. Now the five entries inside their report."""

    @pytest.fixture
    def report(self, db, learning_area) -> D.EqwartReport:
        return EqwartReportRepository(db).add(
            D.EqwartReport(
                learning_area_id=learning_area.id,
                assessment_type=AssessmentType.term,
                school_year="2025-2026",
            )
        )

    def test_encoding_five_and_reading_them_in_order(self, db, report):
        entries = LeastMasteredRepository(db)
        entries.add_all(
            [
                D.LeastMastered(
                    report_id=report.id, position=position, item_no=position * 3
                )
                for position in (3, 1, 5, 2, 4)
            ]
        )
        stored = entries.for_report(report.id)
        assert [entry.position for entry in stored] == [1, 2, 3, 4, 5]

    def test_one_position_can_be_replaced(self, db, report):
        entries = LeastMasteredRepository(db)
        entries.add(D.LeastMastered(report_id=report.id, position=1, item_no=9))
        entries.update(
            entries.at_position(report.id, 1), competency_text="Describes motion"
        )
        assert entries.at_position(report.id, 1).competency_text == "Describes motion"

    def test_a_sixth_position_is_refused(self, db, report):
        """Was `CHECK (position BETWEEN 1 AND 5)`."""
        with pytest.raises(Exception):
            LeastMasteredRepository(db).add(
                D.LeastMastered(report_id=report.id, position=6)
            )


# --------------------------------------------------------------------------- #
# Assessments and submissions                                                  #
# --------------------------------------------------------------------------- #
class TestAssessments:
    def test_by_code_is_how_a_scan_finds_its_assessment(self, db, assessment):
        assert AssessmentRepository(db).by_code("GENSCI-TERM-01").id == assessment.id

    def test_get_full_reaches_the_section_through_the_class(
        self, db, assessment, section
    ):
        """The exporter reads `assessment.class_.section`, so both levels load."""
        loaded = AssessmentRepository(db).get_full(assessment.id)
        assert loaded.class_ is not None
        assert loaded.class_.section is not None
        assert loaded.class_.section.name == section.name

    def test_every_sections_copy_makes_the_columns_of_a_sheet(
        self, db, assessment, learning_area
    ):
        found = AssessmentRepository(db).for_learning_area(
            learning_area.id, AssessmentType.term, "2025-2026"
        )
        assert [record.id for record in found] == [assessment.id]
        assert found[0].class_ is not None

    def test_an_area_with_no_classes_yields_nothing(self, db, assessment):
        assert (
            AssessmentRepository(db).for_learning_area(
                uuid.uuid4(), AssessmentType.term, "2025-2026"
            )
            == []
        )

    def test_for_section_finds_it_through_the_class(self, db, assessment, section):
        found = AssessmentRepository(db).for_section(section.id, "2025-2026")
        assert [record.id for record in found] == [assessment.id]

    def test_marking_in_progress_only_advances_a_draft(self, db, assessment):
        assessments = AssessmentRepository(db)
        assert assessment.status is AssessmentStatus.draft

        assessments.mark_in_progress(assessment)
        assert assessment.status is AssessmentStatus.in_progress

        assessments.update(assessment, status=AssessmentStatus.completed)
        assessments.mark_in_progress(assessment)
        assert assessment.status is AssessmentStatus.completed, "must not go backwards"

    def test_the_date_survives_storage(self, db, assessment):
        """BSON has no date type, so it is widened and narrowed back."""
        from datetime import date

        assert AssessmentRepository(db).get(assessment.id).date == date(2026, 3, 14)


class TestSubmissions:
    def test_the_oldest_paper_awaiting_review_comes_first(
        self, db, assessment, students
    ):
        submissions = SubmissionRepository(db)
        later = submissions.add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[1].id,
                status=SubmissionStatus.review,
                scanned_at=datetime(2026, 3, 14, 10, 30, tzinfo=timezone.utc),
            )
        )
        earlier = submissions.add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[0].id,
                status=SubmissionStatus.review,
                scanned_at=datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc),
            )
        )

        following = submissions.next_awaiting_review(assessment.id)
        assert following.id == earlier.id
        assert following.student is not None

    def test_only_approved_papers_feed_the_statistics(self, db, assessment, students):
        submissions = SubmissionRepository(db)
        submissions.add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[0].id,
                status=SubmissionStatus.approved,
            )
        )
        submissions.add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[1].id,
                status=SubmissionStatus.review,
            )
        )
        assert len(submissions.approved_for_assessment(assessment.id)) == 1
        assert len(submissions.for_assessment(assessment.id)) == 2

    def test_learners_are_filled_in_for_a_whole_batch(self, db, assessment, students):
        submissions = SubmissionRepository(db)
        for student in students:
            submissions.add(
                D.Submission(assessment_id=assessment.id, student_id=student.id)
            )

        loaded = submissions.for_assessment(assessment.id)
        assert len(loaded) == 3
        assert all(record.student is not None for record in loaded)

    def test_approved_types_are_scoped_to_the_class(
        self, db, assessment, klass, students
    ):
        """Decides which certificate someone earned, so it must not over-report."""
        submissions = SubmissionRepository(db)
        submissions.add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[0].id,
                status=SubmissionStatus.approved,
            )
        )

        earned = submissions.approved_types_for_student(students[0].id, klass.id)
        assert earned == {AssessmentType.term}
        assert (
            submissions.approved_types_for_student(students[0].id, uuid.uuid4()) == set()
        )

    def test_an_unapproved_paper_earns_nothing(self, db, assessment, klass, students):
        SubmissionRepository(db).add(
            D.Submission(
                assessment_id=assessment.id,
                student_id=students[0].id,
                status=SubmissionStatus.review,
            )
        )
        assert (
            SubmissionRepository(db).approved_types_for_student(
                students[0].id, klass.id
            )
            == set()
        )


# --------------------------------------------------------------------------- #
# Analysis, reports and the rest                                               #
# --------------------------------------------------------------------------- #
class TestItemAnalysis:
    def test_recomputing_replaces_rather_than_accumulates(self, db, assessment):
        analysis = ItemAnalysisRepository(db)
        rows = [
            D.ItemAnalysis(
                assessment_id=assessment.id, item_no=item, percent_correct=50.0
            )
            for item in range(1, 11)
        ]
        analysis.replace_for_assessment(assessment.id, rows)
        analysis.replace_for_assessment(assessment.id, rows[:5])
        assert len(analysis.for_assessment(assessment.id)) == 5

    def test_the_weakest_items_tie_break_on_item_number(self, db, assessment):
        """Two items at the same percentage must always order the same way, or
        the five a coordinator encodes shuffle between runs of one report."""
        analysis = ItemAnalysisRepository(db)
        analysis.replace_for_assessment(
            assessment.id,
            [
                D.ItemAnalysis(
                    assessment_id=assessment.id, item_no=item, percent_correct=percent
                )
                for item, percent in [(3, 10.0), (1, 10.0), (2, 90.0)]
            ],
        )
        weakest = analysis.weakest(assessment.id, 2)
        assert [row.item_no for row in weakest] == [1, 3]


class TestEqwartReports:
    def test_finding_one_by_area_type_and_year(self, db, learning_area):
        reports = EqwartReportRepository(db)
        report = reports.add(
            D.EqwartReport(
                learning_area_id=learning_area.id,
                assessment_type=AssessmentType.post,
                school_year="2025-2026",
            )
        )
        assert (
            reports.find(learning_area.id, AssessmentType.post, "2025-2026").id
            == report.id
        )
        assert reports.find(learning_area.id, AssessmentType.pre, "2025-2026") is None


class TestCertificates:
    def test_only_released_certificates_are_listed_as_such(
        self, db, klass, students
    ):
        certificates = CertificateRepository(db)
        certificates.add(
            D.Certificate(
                student_id=students[0].id,
                class_id=klass.id,
                type=CertificateType.completion,
                status=CertificateStatus.released,
            )
        )
        certificates.add(
            D.Certificate(
                student_id=students[1].id,
                class_id=klass.id,
                type=CertificateType.participation,
            )
        )
        assert len(certificates.for_class(klass.id)) == 2
        assert len(certificates.released_for_class(klass.id)) == 1


class TestAudit:
    def test_the_trail_reads_newest_first(self, db, teacher):
        audit = AuditRepository(db)
        for action in ("scan", "approve", "release"):
            audit.record(
                actor_id=teacher.id,
                action=action,
                entity_type="submission",
                entity_id=uuid.uuid4(),
            )
        assert [row.action for row in audit.recent()][0] == "release"


class TestOrdering:
    """Order is never implicit in MongoDB, so every repository declares it."""

    def test_sections_order_by_position_for_the_sheet_columns(self, db, school):
        sections = SectionRepository(db)
        for name, position in [("11-3", 3), ("11-1", 1), ("11-2", 2)]:
            sections.add(
                D.Section(
                    school_id=school.id,
                    name=name,
                    school_year="2025-2026",
                    position=position,
                )
            )
        ordered = sections.ordered(school.id, "2025-2026")
        assert [section.name for section in ordered] == ["11-1", "11-2", "11-3"]

    def test_learning_areas_order_by_name(self, db, school):
        areas = LearningAreaRepository(db)
        for code, name in [("Z", "Zoology"), ("A", "Algebra")]:
            areas.add(
                D.LearningArea(
                    school_id=school.id, code=code, name=name, sheet_name=name
                )
            )
        assert [area.name for area in areas.ordered(school.id)] == [
            "Algebra",
            "Zoology",
        ]

    def test_every_repository_declares_a_sort(self):
        """A missing `default_sort` means results can reshuffle between runs."""
        for repository in MongoRepository.__subclasses__():
            assert repository.default_sort, f"{repository.__name__} has no default_sort"
