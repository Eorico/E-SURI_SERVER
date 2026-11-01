"""The school's own starting data.

Declared here, apart from the script that applies it, for one reason: the tests
read it. A `sheet_name` that does not match the worksheet tab in the EQWART
workbook makes the exporter silently skip that subject, and asserting that in a
test means reading the same list the seed writes - not a second copy of it that
can drift out of step.

Applied by `scripts/seed.py`, which is re-runnable.
"""

from __future__ import annotations

SCHOOL = {
    "name": "Paliparan III Senior High School",
    "division": "Division of Dasmariñas City",
    "cluster": "Cluster VIII",
    "school_year": "2026-2027",
}

#: The five the school runs. `code` is ours and is what a subject is matched on.
DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("SOCSCI", "Social Science"),
    ("MATHPE", "Math & PE"),
    ("TECHPRO", "Technical Professional"),
    ("SCIENCE", "Science"),
    ("COMMARTS", "Communication Arts"),
)

#: (code, name, sheet_name, department code).
#:
#: `sheet_name` is the workbook's, not ours: it has to equal the worksheet tab
#: exactly or `eqwart_export` cannot find the sheet to write into. That is why
#: the spellings look inconsistent - "Gen Science" but "E.Comm" - they are
#: transcribed from the DepEd template rather than tidied.
LEARNING_AREAS: tuple[tuple[str, str, str, str | None], ...] = (
    ("GENSCI", "General Science", "Gen Science", "SCIENCE"),
    ("GENMATH", "General Mathematics", "Gen Math", "MATHPE"),
    ("CL", "Contemporary Literature", "CL", "COMMARTS"),
    ("SSTP", "Elective — Social Science", "SSTP", "SOCSCI"),
    ("PKLP", "Pag-aaral ng Kasaysayan ng Lipunang Pilipino", "PKLP", "SOCSCI"),
    ("LCS", "Life and Career Skills", "LCS", "TECHPRO"),
    ("ECOMM", "Effective Communication", "E.Comm", "COMMARTS"),
    ("MKOM", "Mabisang Komunikasyon", "M. Kommunikasyon", "COMMARTS"),
)

#: Ordered as the EQWART sheet's columns B..V are ordered, which is what
#: `position` carries. Eighteen sections against twenty-one columns, so there is
#: room before the sheet runs out.
SECTIONS: tuple[str, ...] = (
    "ACAD ASSH 11-1",
    "ACAD ASSH 11-2",
    "ACAD ASSH 11-3",
    "ACAD STEM 11-1",
    "ACAD STEM 11-2",
    "ACAD STEM 11-3",
    "ACAD ABM 11-1",
    "ACAD ABM 11-2",
    "ACAD HUMSS 11-1",
    "ACAD HUMSS 11-2",
    "ACAD GAS 11-1",
    "ACAD GAS 11-2",
    "TechPro AWH 11-1",
    "TechPro AWH 11-2",
    "TechPro CRT 11-1",
    "TechPro CRT 11-2",
    "TechPro EIM 11-1",
    "TechPro SMAW 11-1",
)

GRADE_LEVEL = 11

#: The cycle the school opens with, so a class created on day one has a term.
FIRST_TERM_NO = 1
