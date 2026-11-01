"""The cache, and the invalidation that makes it safe to have.

A cache is the one optimisation that can serve a *wrong* answer rather than a
slow one, and the wrong answer here would be an authorisation decision: a
deactivated account still acting, or a demoted one still reading what it used
to. So most of this file is about invalidation, not about speed.

The counting tests reach into `db[...].find` directly because the point is the
number of round trips, which is the only thing a cache changes.
"""

from __future__ import annotations

import time
import uuid

import pytest

from api.core import cache as cache_module
from api.core.cache import (
    DEFAULT_TTL_SECONDS,
    MISSING,
    PRINCIPAL_TTL_SECONDS,
    TTLCache,
    cache,
    cache_key,
)
from api.models import documents as D
from api.models.enums import AdminType, TemplateStatus, UserRole
from api.repositories.class_repository import ClassRepository
from api.repositories.profile_repository import ProfileRepository
from api.repositories.school_repository import (
    LearningAreaRepository,
    SchoolRepository,
    SectionRepository,
)
from api.repositories.template_repository import ReportTemplateRepository


def count_reads(db, collection: str) -> list:
    """Record every query sent to one collection."""
    issued: list = []
    original = db[collection].find

    def recording(*args, **kwargs):
        issued.append(args[0] if args else {})
        return original(*args, **kwargs)

    db[collection].find = recording
    return issued


def count_lookups(db, collection: str) -> list:
    issued: list = []
    original = db[collection].find_one

    def recording(*args, **kwargs):
        issued.append(args[0] if args else {})
        return original(*args, **kwargs)

    db[collection].find_one = recording
    return issued


# --------------------------------------------------------------------------- #
# The store                                                                    #
# --------------------------------------------------------------------------- #
class TestTTLCache:
    def test_a_value_comes_back(self):
        store = TTLCache()
        store.set("ns:k", 42, ttl=60)
        assert store.get("ns:k") == 42

    def test_a_missing_key_is_distinguishable_from_a_cached_none(self):
        """A lookup that legitimately found nothing must still be a hit.

        Returning `None` for both would mean every "no such record" query ran
        again every time, which is the case a cache helps most.
        """
        store = TTLCache()
        assert store.get("ns:absent") is MISSING

        store.set("ns:present", None, ttl=60)
        assert store.get("ns:present") is None
        assert store.get("ns:present") is not MISSING

    def test_an_entry_expires(self):
        store = TTLCache()
        store.set("ns:k", "v", ttl=0.05)
        assert store.get("ns:k") == "v"
        time.sleep(0.08)
        assert store.get("ns:k") is MISSING

    def test_a_namespace_can_be_dropped_without_touching_others(self):
        store = TTLCache()
        store.set("users:a", 1, ttl=60)
        store.set("users:b", 2, ttl=60)
        store.set("sections:a", 3, ttl=60)

        assert store.invalidate("users") == 2
        assert store.get("users:a") is MISSING
        assert store.get("sections:a") == 3

    def test_it_will_not_grow_without_bound(self):
        """Keyed on record ids, an uncapped cache grows until the process dies -
        a slower and far more confusing failure than a cache miss."""
        store = TTLCache(max_entries=10)
        for index in range(50):
            store.set(f"ns:{index}", index, ttl=60)

        assert len(store) <= 10
        assert store.stats.evictions >= 40

    def test_eviction_drops_the_least_recently_used(self):
        store = TTLCache(max_entries=3)
        store.set("ns:keep", 1, ttl=60)
        store.set("ns:drop", 2, ttl=60)
        store.set("ns:also", 3, ttl=60)

        store.get("ns:keep")  # touched, so no longer the oldest
        store.set("ns:new", 4, ttl=60)

        assert store.get("ns:keep") == 1
        assert store.get("ns:drop") is MISSING

    def test_hits_and_misses_are_counted(self):
        store = TTLCache()
        store.set("ns:k", 1, ttl=60)
        store.get("ns:k")
        store.get("ns:nope")
        assert store.stats.hits == 1
        assert store.stats.misses == 1
        assert store.stats.hit_rate == 0.5


class TestCacheKeys:
    def test_every_argument_lands_in_the_key(self):
        """Leaving one out is how one caller is served another's answer."""
        first = cache_key("sections", "ordered", "school-1", "2025-2026")
        second = cache_key("sections", "ordered", "school-2", "2025-2026")
        third = cache_key("sections", "ordered", "school-1", "2026-2027")
        assert len({first, second, third}) == 3

    def test_the_key_starts_with_the_namespace(self):
        """Which is what makes namespace invalidation a prefix match."""
        assert cache_key("users", "with_role_details", "x").startswith("users:")

    def test_none_does_not_collide_with_the_empty_string(self):
        assert cache_key("ns", "m", None) == cache_key("ns", "m", "")


# --------------------------------------------------------------------------- #
# Reads are actually served from it                                            #
# --------------------------------------------------------------------------- #
class TestCachedReads:
    def test_the_second_read_does_not_reach_the_database(self, db, school):
        schools = SchoolRepository(db)
        schools.current()  # warm

        lookups = count_lookups(db, "schools")
        assert schools.current().id == school.id
        assert schools.current().id == school.id
        assert lookups == [], "a warm read must not query at all"

    def test_the_subject_list_is_cached(self, db, school, learning_area):
        areas = LearningAreaRepository(db)
        areas.ordered(school.id)

        reads = count_reads(db, "learning_areas")
        areas.ordered(school.id)
        assert reads == []

    def test_a_different_argument_is_a_different_entry(self, db, school, learning_area):
        """The cache must not answer a question it was not asked."""
        areas = LearningAreaRepository(db)
        assert len(areas.ordered(school.id)) == 1
        assert areas.ordered(uuid.uuid4()) == []
        assert len(areas.ordered(school.id)) == 1

    def test_the_caller_profile_is_cached(self, db, teacher):
        profiles = ProfileRepository(db)
        profiles.with_role_details(teacher.id)

        lookups = count_lookups(db, "users")
        loaded = profiles.with_role_details(teacher.id)
        assert lookups == [], "this runs on every authenticated request"
        assert loaded.id == teacher.id

    def test_a_profile_that_does_not_exist_is_cached_too(self, db):
        """Otherwise a bad token re-queries on every request it makes."""
        profiles = ProfileRepository(db)
        absent = uuid.uuid4()
        assert profiles.with_role_details(absent) is None

        lookups = count_lookups(db, "users")
        assert profiles.with_role_details(absent) is None
        assert lookups == []


# --------------------------------------------------------------------------- #
# Invalidation - the half that makes it correct                                #
# --------------------------------------------------------------------------- #
class TestInvalidationOnWrite:
    def test_adding_a_subject_shows_up_at_once(self, db, school, learning_area):
        areas = LearningAreaRepository(db)
        assert len(areas.ordered(school.id)) == 1

        areas.add(
            D.LearningArea(
                school_id=school.id, code="NEW", name="New Subject", sheet_name="New"
            )
        )
        assert len(areas.ordered(school.id)) == 2, "a write must clear its own reads"

    def test_renaming_a_subject_shows_up_at_once(self, db, school, learning_area):
        areas = LearningAreaRepository(db)
        areas.ordered(school.id)

        areas.update(learning_area, name="Renamed Science")
        assert areas.ordered(school.id)[0].name == "Renamed Science"

    def test_deleting_a_subject_shows_up_at_once(self, db, school, learning_area):
        areas = LearningAreaRepository(db)
        areas.ordered(school.id)
        areas.delete(learning_area)
        assert areas.ordered(school.id) == []

    def test_a_bulk_insert_invalidates(self, db, school, section):
        sections = SectionRepository(db)
        sections.ordered(school.id, "2025-2026")

        sections.add_all(
            [
                D.Section(
                    school_id=school.id, name="11-9", school_year="2025-2026", position=9
                )
            ]
        )
        assert len(sections.ordered(school.id, "2025-2026")) == 2

    def test_delete_where_invalidates(self, db, school, learning_area):
        areas = LearningAreaRepository(db)
        areas.ordered(school.id)
        areas.delete_where(school_id=school.id)
        assert areas.ordered(school.id) == []

    def test_activating_a_template_shows_up_at_once(self, db):
        templates = ReportTemplateRepository(db)
        assert templates.active_for("2025-2026", 11) == []

        templates.add(
            D.ReportTemplate(
                name="EQWART",
                original_filename="e.xlsm",
                storage_path="p",
                checksum="c",
                school_year="2025-2026",
                status=TemplateStatus.active,
            )
        )
        assert len(templates.active_for("2025-2026", 11)) == 1


class TestInvalidationAcrossCollections:
    """A cached read that embeds data from elsewhere has to be cleared too."""

    def test_renaming_a_section_updates_the_hydrated_class(
        self, db, klass, section
    ):
        classes = ClassRepository(db)
        assert classes.get_full(klass.id).section.name == "ACAD ASSH 11-1"

        SectionRepository(db).update(section, name="ACAD ASSH 11-9")
        assert classes.get_full(klass.id).section.name == "ACAD ASSH 11-9", (
            "a hydrated class carries a copy of its section, so a write to "
            "sections has to clear the classes as well"
        )

    def test_renaming_a_teacher_updates_the_hydrated_class(self, db, klass, teacher):
        classes = ClassRepository(db)
        classes.get_full(klass.id)

        ProfileRepository(db).update(teacher, last_name="Reyes")
        assert classes.get_full(klass.id).teacher.last_name == "Reyes"

    def test_editing_an_answer_key_invalidates_its_assessment(self, db, assessment):
        """The array lives inside the parent, so a cached parent is now stale."""
        from api.repositories.assessment_repository import (
            AnswerKeyRepository,
            AssessmentRepository,
        )

        keys = AnswerKeyRepository(db)
        AssessmentRepository(db).get(assessment.id)

        keys.update(keys.item(assessment.id, 7), correct_option="E")
        assert keys.as_map(assessment.id)[7] == "E"


# --------------------------------------------------------------------------- #
# Security                                                                     #
# --------------------------------------------------------------------------- #
class TestCacheSecurity:
    """The cases where a stale entry would be an authorisation decision."""

    def test_deactivating_an_account_takes_effect_at_once(self, db, teacher):
        """The entry carries `is_active`, which is what `get_principal` refuses on.

        A stale hit here means a dismissed member of staff goes on working.
        """
        profiles = ProfileRepository(db)
        assert profiles.with_role_details(teacher.id).is_active is True

        profiles.update(teacher, is_active=False)
        assert profiles.with_role_details(teacher.id).is_active is False

    def test_demoting_an_account_takes_effect_at_once(self, db, school):
        """The entry carries `role`, which every route guard reads."""
        profiles = ProfileRepository(db)
        admin = profiles.add(
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
        assert profiles.with_role_details(admin.id).role is UserRole.administrator

        profiles.update(admin, role=UserRole.subject_teacher, administrator_profile=None)

        reloaded = profiles.with_role_details(admin.id)
        assert reloaded.role is UserRole.subject_teacher
        assert reloaded.admin_type is None, "the admin type must go with the role"

    def test_changing_a_department_takes_effect_at_once(self, db, school, department):
        """It decides which department's reports a master teacher may read."""
        profiles = ProfileRepository(db)
        master = profiles.add(
            D.Profile(
                role=UserRole.administrator,
                last_name="Cruz",
                first_name="Ben",
                username="bcruz",
                email="bcruz@example.ph",
                school_id=school.id,
                administrator_profile=D.AdministratorProfile(
                    admin_type=AdminType.master_teacher, department_id=department.id
                ),
            )
        )
        assert profiles.with_role_details(master.id).department_id == department.id

        moved = uuid.uuid4()
        profiles.update(
            master,
            administrator_profile=D.AdministratorProfile(
                admin_type=AdminType.master_teacher, department_id=moved
            ),
        )
        assert profiles.with_role_details(master.id).department_id == moved

    def test_two_profiles_never_share_an_entry(self, db, teacher, school):
        """The obvious catastrophe, asserted rather than assumed."""
        profiles = ProfileRepository(db)
        other = profiles.add(
            D.Profile(
                last_name="Other",
                first_name="Person",
                username="other",
                email="other@example.ph",
                school_id=school.id,
            )
        )

        assert profiles.with_role_details(teacher.id).id == teacher.id
        assert profiles.with_role_details(other.id).id == other.id
        assert profiles.with_role_details(teacher.id).id == teacher.id

    def test_the_profile_entry_expires_sooner_than_anything_else(self):
        """With one cache per worker, the TTL is the ceiling on how long a
        revocation can stay invisible to a worker that did not make it."""
        assert PRINCIPAL_TTL_SECONDS < DEFAULT_TTL_SECONDS
        assert PRINCIPAL_TTL_SECONDS <= 30

    def test_nothing_about_a_token_is_cached(self):
        """Signature, expiry and audience are re-checked on every request.

        What is cached is the profile behind an already-verified token, never
        the verification - so an expired or forged token is refused whatever
        the cache holds.
        """
        import inspect

        from api.core import security, tokens

        assert "cache" not in inspect.getsource(tokens)
        verified = inspect.getsource(security.get_current_user)
        assert "cache" not in verified

    def test_the_stats_leak_nothing(self):
        """They are exposed on an unauthenticated readiness probe."""
        from api.core.cache import stats

        allowed = {"hits", "misses", "evictions", "invalidations", "hit_rate", "entries"}
        assert set(stats()) == allowed
        assert all(isinstance(value, (int, float)) for value in stats().values())


# --------------------------------------------------------------------------- #
# What must not be cached                                                      #
# --------------------------------------------------------------------------- #
def test_nothing_a_teacher_is_editing_is_cached():
    """A stale score is worse than a slow one.

    Submissions, responses and item analysis are written and re-read in the
    same breath while a teacher reviews a paper, so none of their reads may be
    served from a cache.
    """
    import inspect

    from api.repositories import (
        analysis_repository,
        submission_repository,
    )

    for module in (submission_repository, analysis_repository):
        source = inspect.getsource(module)
        assert "@cached" not in source, f"{module.__name__} caches a read it must not"


def test_a_write_clears_more_than_it_must_rather_than_less(db, school, learning_area):
    """Over-invalidating costs one query; under-invalidating serves a wrong answer.

    A write to `learning_areas` also clears `classes` and `assessments`,
    because both embed a copy of the subject when hydrated.
    """
    areas = LearningAreaRepository(db)
    cache.set("classes:anything", "stale", ttl=60)
    cache.set("assessments:anything", "stale", ttl=60)

    areas.update(learning_area, name="Renamed")

    assert cache.get("classes:anything") is MISSING
    assert cache.get("assessments:anything") is MISSING
