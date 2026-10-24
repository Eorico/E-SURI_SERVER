"""OMR scanning pipeline, server-side OpenCV.

ISP: small interfaces (`CodeReader`, `BubbleDetector`) instead of one fat class.
`Scanner` composes them, so each piece is swappable and independently testable.

The pipeline is: decode → threshold → find the corner fiducials → warp to a
canonical rectangle → read the codes → sample every bubble from
`SheetLayout`. Because the layout is shared with the generator, a bubble is
always sampled exactly where it was printed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from utils.omr.sheet_layout import SheetLayout

#: The deskewed working image. Large enough that a 4mm bubble is ~25px across.
CANONICAL_WIDTH = 1240
CANONICAL_HEIGHT = 1754  # A4 at ~150 dpi

#: A bubble counts as shaded when this share of its area is dark.
FILL_THRESHOLD = 0.45
#: A second mark this close to the darkest one means the learner shaded both.
AMBIGUITY_RATIO = 0.80


@dataclass
class ScannedItem:
    item_no: int
    marked_option: str | None  # None when blank
    flag: str  # "none" | "blank" | "multiple"
    fill_ratios: dict[str, float] | None = None


@dataclass
class ScanResult:
    test_code: str | None
    student_no: str | None
    items: list[ScannedItem]
    warped: np.ndarray | None = None


class SheetNotFoundError(ValueError):
    """The four corner marks could not be located."""


class CodeReader(Protocol):
    def read(self, warped: np.ndarray) -> tuple[str | None, str | None]: ...


class BubbleDetector(Protocol):
    def detect(self, warped: np.ndarray, layout: SheetLayout) -> list[ScannedItem]: ...


# --------------------------------------------------------------------------- #
# Implementations                                                              #
# --------------------------------------------------------------------------- #
class ZbarCodeReader:
    """Reads the QR/barcode carrying the test code and student number.

    `pyzbar` needs a native library that is not always present, so the import is
    deferred: without it the scanner still works and the caller supplies the
    student number instead.
    """

    def read(self, warped: np.ndarray) -> tuple[str | None, str | None]:
        try:
            from pyzbar import pyzbar  # noqa: PLC0415 — optional dependency
        except Exception:  # pragma: no cover - depends on the host
            return None, None

        for symbol in pyzbar.decode(warped):
            payload = symbol.data.decode("utf-8", errors="ignore").strip()
            if not payload:
                continue
            # Encoded as "TESTCODE|STUDENTNO" by the sheet generator.
            if "|" in payload:
                test_code, _, student_no = payload.partition("|")
                return test_code.strip() or None, student_no.strip() or None
            return payload, None
        return None, None


class GridBubbleDetector:
    """Samples each printed bubble position and decides whether it is shaded."""

    def __init__(
        self,
        fill_threshold: float = FILL_THRESHOLD,
        ambiguity_ratio: float = AMBIGUITY_RATIO,
    ):
        self.fill_threshold = fill_threshold
        self.ambiguity_ratio = ambiguity_ratio

    def detect(self, warped: np.ndarray, layout: SheetLayout) -> list[ScannedItem]:
        binary = self._binarise(warped)
        height, width = binary.shape[:2]
        radius = self._bubble_radius(layout, width)

        items: list[ScannedItem] = []
        for item_no, bubbles in sorted(layout.grouped_bubbles().items()):
            ratios = {
                bubble.option: self._fill_ratio(
                    binary, int(bubble.x * width), int(bubble.y * height), radius
                )
                for bubble in bubbles
            }
            items.append(self._classify(item_no, ratios))
        return items

    # --- internals --------------------------------------------------------
    @staticmethod
    def _binarise(warped: np.ndarray) -> np.ndarray:
        """Adaptive threshold copes with uneven lighting from a phone camera."""
        grey = warped if warped.ndim == 2 else cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(grey, (5, 5), 0)
        return cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
        )

    @staticmethod
    def _bubble_radius(layout: SheetLayout, image_width: int) -> int:
        """The warped image spans the fiducial box, not the whole page."""
        share = layout.bubble_diameter_mm / layout.box_width_mm
        return max(4, int(image_width * share / 2))

    @staticmethod
    def _fill_ratio(binary: np.ndarray, cx: int, cy: int, radius: int) -> float:
        """Share of the bubble's disc that is dark."""
        height, width = binary.shape[:2]
        x0, x1 = max(0, cx - radius), min(width, cx + radius + 1)
        y0, y1 = max(0, cy - radius), min(height, cy + radius + 1)
        if x1 <= x0 or y1 <= y0:
            return 0.0

        patch = binary[y0:y1, x0:x1]
        mask = np.zeros(patch.shape[:2], dtype=np.uint8)
        cv2.circle(mask, (patch.shape[1] // 2, patch.shape[0] // 2), radius, 255, -1)
        area = int(np.count_nonzero(mask))
        if area == 0:
            return 0.0
        return float(np.count_nonzero(cv2.bitwise_and(patch, patch, mask=mask))) / area

    def _classify(self, item_no: int, ratios: dict[str, float]) -> ScannedItem:
        ordered = sorted(ratios.items(), key=lambda pair: pair[1], reverse=True)
        best_option, best_ratio = ordered[0]
        runner_ratio = ordered[1][1] if len(ordered) > 1 else 0.0

        if best_ratio < self.fill_threshold:
            return ScannedItem(item_no, None, "blank", ratios)

        if runner_ratio >= self.fill_threshold and runner_ratio >= best_ratio * self.ambiguity_ratio:
            shaded = "".join(
                option for option, ratio in ordered if ratio >= self.fill_threshold
            )
            return ScannedItem(item_no, shaded, "multiple", ratios)

        return ScannedItem(item_no, best_option, "none", ratios)


class Scanner:
    """Orchestrates preprocess → locate/deskew → read code → detect bubbles."""

    def __init__(self, code_reader: CodeReader, bubble_detector: BubbleDetector):
        self.code_reader = code_reader
        self.bubble_detector = bubble_detector

    def scan(self, image_bytes: bytes, layout: SheetLayout) -> ScanResult:
        image = self._decode(image_bytes)
        warped = self._deskew(image)
        test_code, student_no = self.code_reader.read(warped)
        items = self.bubble_detector.detect(warped, layout)
        return ScanResult(test_code=test_code, student_no=student_no, items=items, warped=warped)

    # --- internals --------------------------------------------------------
    @staticmethod
    def _decode(image_bytes: bytes) -> np.ndarray:
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise SheetNotFoundError("That file is not a readable image.")
        return image

    def _deskew(self, image: np.ndarray) -> np.ndarray:
        """Warp the sheet onto a canonical rectangle using the corner marks."""
        corners = self._find_fiducials(image)
        if corners is None:
            # Fall back to the page outline, which is usually good enough for a
            # flat scan even when the fiducials are cropped.
            corners = self._find_page(image)
        if corners is None:
            raise SheetNotFoundError(
                "Could not find the four corner marks. Lay the sheet flat and keep all corners in frame."
            )

        target = np.array(
            [[0, 0], [CANONICAL_WIDTH - 1, 0],
             [CANONICAL_WIDTH - 1, CANONICAL_HEIGHT - 1], [0, CANONICAL_HEIGHT - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(corners, target)
        return cv2.warpPerspective(image, matrix, (CANONICAL_WIDTH, CANONICAL_HEIGHT))

    @staticmethod
    def _order_corners(points: np.ndarray) -> np.ndarray:
        """Top-left, top-right, bottom-right, bottom-left."""
        ordered = np.zeros((4, 2), dtype=np.float32)
        total = points.sum(axis=1)
        diff = np.diff(points, axis=1).ravel()
        ordered[0] = points[np.argmin(total)]
        ordered[2] = points[np.argmax(total)]
        ordered[1] = points[np.argmin(diff)]
        ordered[3] = points[np.argmax(diff)]
        return ordered

    def _find_fiducials(self, image: np.ndarray) -> np.ndarray | None:
        """Locate the solid black squares nearest each corner."""
        blurred = cv2.GaussianBlur(image, (5, 5), 0)
        binary = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 51, 12
        )
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        height, width = image.shape[:2]
        page_area = float(height * width)
        candidates: list[tuple[float, float]] = []

        for contour in contours:
            area = cv2.contourArea(contour)
            # A fiducial is a small, solid, roughly square blob.
            if not (page_area * 0.00005 < area < page_area * 0.005):
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if h == 0 or not 0.65 <= w / h <= 1.55:
                continue
            if area / float(w * h) < 0.65:
                continue
            candidates.append((x + w / 2.0, y + h / 2.0))

        if len(candidates) < 4:
            return None

        points = np.array(candidates, dtype=np.float32)
        anchors = np.array(
            [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32
        )
        chosen = [points[int(np.argmin(np.linalg.norm(points - anchor, axis=1)))] for anchor in anchors]
        corners = np.array(chosen, dtype=np.float32)

        # Four distinct marks, or the sheet is not framed properly.
        if len({(round(x), round(y)) for x, y in corners}) < 4:
            return None
        return self._order_corners(corners)

    def _find_page(self, image: np.ndarray) -> np.ndarray | None:
        """Largest four-sided contour — the sheet against a darker background."""
        blurred = cv2.GaussianBlur(image, (5, 5), 0)
        edges = cv2.Canny(blurred, 60, 180)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        page_area = float(image.shape[0] * image.shape[1])
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            if cv2.contourArea(contour) < page_area * 0.25:
                break
            approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
            if len(approx) == 4:
                return self._order_corners(approx.reshape(4, 2).astype(np.float32))
        return None


def build_scanner() -> Scanner:
    """The wiring used by the API; tests inject their own doubles."""
    return Scanner(code_reader=ZbarCodeReader(), bubble_detector=GridBubbleDetector())
