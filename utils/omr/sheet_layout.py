"""The single source of truth for answer-sheet geometry.

Both the sheet generator and the scanner read this, so the printed layout and
the scan layout can never drift apart (DRY).

Positions are given in a normalised space: (0,0) is the top-left fiducial centre
and (1,1) the bottom-right one. The generator scales that onto the page in
millimetres; the scanner scales it onto the deskewed image in pixels. Neither
needs to know the other's units.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Items are laid out in columns of at most this many rows, as on the reference
#: sheets (60 items become 21 + 21 + 18).
MAX_ROWS_PER_COLUMN = 21


@dataclass(frozen=True)
class BubblePosition:
    item_no: int
    option: str
    x: float
    y: float


@dataclass(frozen=True)
class SheetLayout:
    """Where every bubble sits, for one item count and option set."""

    item_count: int
    options: tuple[str, ...] = ("A", "B", "C", "D", "E")

    # --- page geometry, millimetres (A4 portrait) -------------------------
    page_width_mm: float = 210.0
    page_height_mm: float = 297.0
    margin_mm: float = 14.0
    fiducial_mm: float = 6.0
    bubble_diameter_mm: float = 4.2

    # --- the grid, as fractions of the fiducial box ----------------------
    #: Top of the first bubble row, below the name/section/quiz boxes.
    grid_top: float = 0.22
    grid_bottom: float = 0.95
    grid_left: float = 0.06
    grid_right: float = 0.97
    #: Width of one option cell relative to a column's width.
    option_span: float = 0.62

    #: Where the bubbled student-ID grid sits, and how many digits it holds.
    student_id_digits: int = 6

    _positions: list[BubblePosition] = field(default_factory=list, repr=False, compare=False)

    # --- construction -----------------------------------------------------
    @classmethod
    def for_item_count(cls, item_count: int, options: str | tuple[str, ...] = "ABCDE") -> "SheetLayout":
        if isinstance(options, str):
            options = tuple(options)
        return cls(item_count=item_count, options=options)

    @classmethod
    def for_assessment_type(cls, assessment_type: str) -> "SheetLayout":
        """30 items for summative (st1/st2), 60 for pre/post/term."""
        return cls.for_item_count(30 if assessment_type in {"st1", "st2"} else 60)

    # --- derived shape ----------------------------------------------------
    @property
    def column_count(self) -> int:
        return max(1, -(-self.item_count // MAX_ROWS_PER_COLUMN))

    @property
    def rows_per_column(self) -> int:
        return -(-self.item_count // self.column_count)

    def position_of(self, item_no: int) -> tuple[int, int]:
        """(column, row), both zero-based, for an item number."""
        index = item_no - 1
        return index // self.rows_per_column, index % self.rows_per_column

    # --- bubble centres ---------------------------------------------------
    def bubbles(self) -> list[BubblePosition]:
        """Every bubble centre in normalised coordinates, cached after first use."""
        if self._positions:
            return self._positions

        positions: list[BubblePosition] = []
        column_width = (self.grid_right - self.grid_left) / self.column_count
        row_height = (self.grid_bottom - self.grid_top) / max(1, self.rows_per_column)
        option_width = (column_width * self.option_span) / len(self.options)
        #: Leave room at the left of each column for the printed item number.
        number_gutter = column_width * (1 - self.option_span) * 0.75

        for item_no in range(1, self.item_count + 1):
            column, row = self.position_of(item_no)
            row_y = self.grid_top + row_height * (row + 0.5)
            column_x = self.grid_left + column_width * column + number_gutter

            for index, option in enumerate(self.options):
                positions.append(
                    BubblePosition(
                        item_no=item_no,
                        option=option,
                        x=column_x + option_width * (index + 0.5),
                        y=row_y,
                    )
                )

        object.__setattr__(self, "_positions", positions)
        return positions

    def bubbles_for_item(self, item_no: int) -> list[BubblePosition]:
        return [bubble for bubble in self.bubbles() if bubble.item_no == item_no]

    def grouped_bubbles(self) -> dict[int, list[BubblePosition]]:
        grouped: dict[int, list[BubblePosition]] = {}
        for bubble in self.bubbles():
            grouped.setdefault(bubble.item_no, []).append(bubble)
        return grouped

    # --- millimetre helpers for the generator -----------------------------
    @property
    def box_width_mm(self) -> float:
        """Width between the left and right fiducial centres."""
        return self.page_width_mm - 2 * self.margin_mm

    @property
    def box_height_mm(self) -> float:
        return self.page_height_mm - 2 * self.margin_mm

    def to_mm(self, x: float, y: float) -> tuple[float, float]:
        """Normalised point to millimetres from the bottom-left of the page.

        ReportLab's origin is bottom-left, so the y axis is flipped here.
        """
        mm_x = self.margin_mm + x * self.box_width_mm
        mm_y = self.margin_mm + (1 - y) * self.box_height_mm
        return mm_x, mm_y
