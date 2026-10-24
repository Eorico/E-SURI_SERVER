# IMECAR / E-SURI — system logic

Extracted from `Capstone Concept.docx` and `TERM 1 EXAM_ GRADE 11.xlsm`.
This file is the contract the backend is built against; when the two sources
disagree with the frontend prototype, this file wins.

---

## 1. Context

**IMECAR** — an Integrated Web-Based Examination Checker and Analysis Report Tool
that produces the **EQWART** (Electronic Quarterly Written Assessment Result Tool)
for **Paliparan III Senior High School**, Division of Dasmariñas City, Cluster VIII.

Grade 11. Eight learning areas in the supplied workbook:

| Sheet | Learning area |
| --- | --- |
| `Gen Science` | General Science |
| `Gen Math` | General Mathematics |
| `CL` | Contemporary Literature |
| `SSTP` | Elective — Social Science |
| `PKLP` | Pag-aaral ng Kasaysayan ng Lipunang Pilipino |
| `LCS` | Life and Career Skills |
| `E.Comm` | Effective Communication |
| `M. Kommunikasyon` | Mabisang Komunikasyon |

Sections are named like `ACAD ASSH 11-1`, `ACAD STEM 11-2`, `TechPro AWH 11-1`.
Core learning areas run 18 sections; electives run 2–4. The workbook allows a
maximum of **21 sections** per learning area (columns B–V).

## 2. Roles

| Role | Can |
| --- | --- |
| `subject_teacher` | Own account. Create sections/classes. Create assessments with answer sheets. Scan and check sheets with the camera, seeing the score live. View their classes, assessments, item analysis and student scores. |
| `subject_coordinator` | Everything a subject teacher can, plus: monitor and track the reports of the subject teachers in their learning area, and **encode the least-mastered competencies** and the intervention plan from the result. |
| `administrator` | View all reports for every class handled by every subject teacher. View and download the overall EQWART workbook. **Release certificates**. |

## 3. Assessments

Five types per class, per school year:

| Type | Label | Items |
| --- | --- | --- |
| `pre` | Pre-Test | **60** |
| `st1` | Summative Test 1 | **30** |
| `st2` | Summative Test 2 | **30** |
| `term` | End Term Examination | **60** |
| `post` | Post-Test | **60** |

- The item count is **fixed by type** — never entered by hand.
- Every assessment gets a **unique test code**, printed on the sheet and read back
  when scanning.
- Answer options are **A–E** (five), matching the printed sheets.

## 4. Answer sheet

Modelled on the reference sheets in the concept doc:

- `Name`, `Class`/`Section` and `Quiz` write-in boxes at the top.
- Solid black square **fiducials** at the four corners and the mid-edges, used to
  locate and deskew the sheet.
- A vertical **sheet-ID barcode** down the left margin.
- Assessment title and test code printed vertically on the right, e.g. `Final Exam (2614)`.
- Items in three columns, five bubbles (A–E) each.
- A bubbled **Student ID** grid (digits 0–9) so a sheet can be matched to a student
  without handwriting recognition.
- 1 sheet per page for 60 items; 30-item sheets may print 2 per page.

## 5. Scanning and scoring

- The teacher scans with a camera; the score appears immediately.
- Each response is classified: a single mark, **blank**, or **multiple** marks.
- Blank and multiple are **flagged** for review; they score 0 until resolved.
- 1 point per item, so `possible_points == item_count`.
- The scanned image is stored and shown back with the marks overlaid — correct
  answers ringed, wrong marks crossed. The cropped **name strip** is the thumbnail
  in the graded-papers list.
- A submission is `review` while anything is flagged, `approved` once confirmed,
  and `missing` until a sheet arrives.
- Only **approved** submissions feed the statistics.

## 6. Statistics

### Per paper
Earned points, possible points, percent correct, scanned timestamp, and a per-item
table of primary answer vs the student's mark.

### Per assessment
Number of papers, number of questions, possible points, and minimum / maximum /
average / median / standard deviation as both score and percent.

### Per item
Correct count, percent correct, **discrimination factor**, and the response
distribution across options including blank (`_`) and multiple (e.g. `CD`).

Discrimination index is the standard upper-27% / lower-27% difference:

```
D = (correct_in_upper_group - correct_in_lower_group) / group_size
```

## 7. Proficiency bands — single source of truth

Applied to an **item's percent correct** (EQWART column Z):

| Percent correct | Level of mastery | Instructional assistance |
| --- | --- | --- |
| ≤ 24 | Not Proficient | Reteach |
| ≤ 49 | Low Proficient | Remediation |
| ≤ 74 | Nearly Proficient | Intervention |
| ≤ 89 | Proficient | Enhancement |
| else | Highly Proficient | Advancement |

Applied to a **learner's raw score** (EQWART rows 194–198), the same bands
expressed as a share of the item count — exact for 60 items:

| Share of total | Band | 60 items | 30 items |
| --- | --- | --- | --- |
| ≤ 25% | Not Proficient | 0–15 | 0–7 |
| ≤ 50% | Low Proficient | 16–30 | 8–15 |
| ≤ 75% | Nearly Proficient | 31–45 | 16–22 |
| ≤ 90% | Proficient | 46–54 | 23–27 |
| else | Highly Proficient | 55–60 | 28–30 |

"Got 75% and above" is counted as **at or above the Proficient cut** (≥ 46 of 60),
mirroring the template's `COUNTIFS(…,">="&$B$213)`.

## 8. EQWART workbook

One sheet per learning area, all with identical geometry.

### Header
| Cell | Value |
| --- | --- |
| `B1` | Division |
| `B2` | Cluster |
| `B3` | School |
| `B4` | School year |
| `B5` | Learning area |
| `B6` | Grade level |
| `B7` | Total number of items |
| `B8` | `=W14` total examinees |
| `B9` | Total sections |

### Table 1 — item analysis, frequency of correct responses
| Row / column | Meaning |
| --- | --- |
| row 13, cols B–V | Section names (max 21) |
| row 14, cols B–V | Examinees per section; `W14 = SUM(B14:V14)` |
| rows 16–75, col A | Item number 1…60 |
| rows 16–75, cols B–V | Count of correct responses per section |
| col W | `=IF(SUM(B:V)=0,"",SUM(B:V))` |
| col X | `=ROUND(W/$W$14*100, 2)` — percent correct |
| col Y | `=RANK(X,$X$16:$X$75,0)` — descending, so rank 60 is weakest |
| col Z | Level of mastery from col X |
| col AA | Instructional assistance from col Z |
| row 76 | `SUM OF ITEM ANALYSIS` per section |
| row 77 | `MEAN` = sum ÷ examinees |
| row 78 | `SD` = `STDEV` over the item frequencies |
| row 79 | `MPS` = mean ÷ total items × 100 |
| `X76:AA79` | Level of mastery of the overall MPS |

### Least mastered competencies — rows 82–88
Top five by rank, auto-filled from Table 1. The **competency text is encoded by
the Coordinator** (`E84:E88`), e.g. *"Solve routine and localized real-life
problems involving…"*.

### Intervention — rows 89–97
*Planned intervention/s* — **multi-select**: Reteaching · Using alternative
instructional strategies · Task analysis · Additional practice · One-on-one
tutoring · Other (specify).

*Mode of intervention* — **choose one**: Add-on (outside class hours) ·
Pull-out (during a regular class, elsewhere) · Built-in (within class hours).

### Table 2 — raw scores, rows 117–206
| Row | Meaning |
| --- | --- |
| 120 | Section names, mirrored from row 13 |
| 121 | Examinee counts, mirrored from row 14 |
| 123–192 | Examinee 1…70, raw score per section |
| 193 | Number of examinees `=COUNTA` |
| 194–198 | Counts per band, Highly Proficient → Not Proficient |
| 199 | Sum of scores |
| 200 | Mean |
| 201 | SD |
| 202 | MPS |
| 203 | Number at or above 75% |
| 204 | Number below 75% |
| 205 | Highest score |
| 206 | Lowest score |
| 208–214 | Band legend |

### Signatories — rows 101–112
Learning Area Key Teacher · Subject Teachers · Reviewed by Master Teacher /
Head Teacher · Noted and Approved for Submission to **CSDO** by the School Head.

### Export rule
Fill a **blanked copy of the supplied template**: write only the encoded values
(section names, examinee counts, item frequencies, raw scores, competencies,
interventions, signatories) and leave every formula, format and merged range
intact so Excel recalculates MPS, SD and rank on open.

## 9. Certificates

Issued per student, per class, by the Administrator:

- **Completion** — every one of the five assessment types has an approved
  submission for that student.
- **Participation** — otherwise.

## 10. Summary reports

1. Summary of Item Analysis
2. Consolidation Report to CSDO

---

## Deviations from the prototype frontend

The frontend was built from Figma screens before these requirements arrived. It
is realigned to this document: Grade 11 and the eight learning areas replace
Grade 8 Mathematics, A–E replaces A–D, 30/60 items replace 50, the five
proficiency bands replace the 75% benchmark, the Advisory workspace becomes the
Administrator, and the Subject Head workspace becomes the Subject Coordinator.

---

## 11. Report templates (added)

DepEd reissues the item-analysis workbook most years, and sheets, sections and
grade levels change with it. The system therefore stores **no fixed cell
addresses**.

### Flow

1. An **administrator** uploads the year's workbook (`POST /admin/report-templates`).
   Only the administrator role can reach any template endpoint.
2. The server builds a **structure digest** — sheet names, printed labels,
   formula shapes, merged ranges, and a count of how many numeric cells sit in
   each row. It contains no learner name or score.
3. A mapping is produced from that digest:
   - by **OpenAI** when `OPENAI_API_KEY` is set, one call per distinct layout
     (a nine-sheet workbook with two layouts costs two calls);
   - by the **heuristic analyser** otherwise, which locates anchors from their
     printed labels and formulas.
   Whichever wins, the result is schema-checked and validated.
4. The mapping is **dry-run**: a copy of the workbook is filled with synthetic
   values and read back. A mapping that does not write and read correctly is
   recorded as failed and **cannot be activated**.
5. The administrator reviews the detected mapping, then activates it. Activating
   archives whatever it replaces.
6. Every export fills the active template through its stored mapping.

### Privacy

The AI sees structure only. Student names and scores never leave the server,
which matters because these are minors' assessment records. Exports are
deterministic — the same data always produces the same workbook.

### Resilience

- No API key: the heuristic analyser handles the work alone.
- AI unavailable or its proposal fails validation: the heuristic result is used.
- Mapping wrong: an administrator can correct it by hand
  (`PUT /admin/report-templates/{id}/mapping`), and it still has to pass the dry run.
- Key added later: `POST /admin/report-templates/{id}/remap` re-reads the stored file.

### Formula defects

The analyser also detects broken comparisons in the band-count rows and records
the corrections on the mapping, so they are repaired on every export:

- a cut-off reference carrying a stray digit (`$B$2111` for `$B$211`), which
  points at an empty cell and silently zeroes a band;
- `">"` where its sibling rows use `">="`, which drops anyone sitting exactly on
  the cut.

Both are present in the supplied `TERM 1 EXAM_ GRADE 11.xlsm` — 168 and 105
cells respectively.
