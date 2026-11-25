"""Immutable domain data contracts for lightweight Cadence OA Symbol snapshots.

Models real Cadence Virtuoso IC26.1 ``schematicSymbol`` CellViews:
- Pin contracts (terminals + pin figures such as ``rect|pin:drawing`` with ``bBox``,
  bus pin names like ``DOUTP<9:0>`` / ``S<2:0>``, or optional instance pin masters).
- Ordered ``port_order`` (OA ``ILList`` property ``portOrder``) and cellview
  properties including scalar primitives and ordered scalar ``ILList`` values
  (such as ``termOrder``).
- Visual symbol graphics separated from pin contracts: shapes (``line``, ``rect``,
  ``polygon``, ``path``, ``arc``, ``ellipse``, ``circle``), typed labels
  (``normalLabel``, ``NLPLabel``, ``ILLabel``), ``selection_box``
  (``rect|instance:drawing``), ``origin``, and optional ``bbox``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .model import (
    SYMBOL_SCHEMA_V1,
    CellViewRef,
    Manifest,
    OmissionRecord,
)

Point2D = tuple[float, float]
BBox = tuple[Point2D, Point2D]
SymbolPropertyScalar = str | int | float | bool
SymbolPropertyValue = SymbolPropertyScalar | list[SymbolPropertyScalar]

PinFigureKind = Literal["rect", "polygon", "ellipse", "circle", "inst"]
SymbolShapeKind = Literal["line", "rect", "polygon", "path", "arc", "ellipse", "circle"]
SymbolShapeInputKind = Literal[
    "line", "rect", "rectangle", "polygon", "path", "arc", "ellipse", "circle"
]
SymbolLabelType = Literal["normalLabel", "NLPLabel", "ILLabel"]

VALID_PIN_FIGURE_KINDS: frozenset[str] = frozenset(
    {"rect", "polygon", "ellipse", "circle", "inst"}
)
VALID_SYMBOL_SHAPE_KINDS: frozenset[str] = frozenset(
    {"line", "rect", "polygon", "path", "arc", "ellipse", "circle"}
)
VALID_SYMBOL_LABEL_TYPES: frozenset[str] = frozenset(
    {"normalLabel", "NLPLabel", "ILLabel"}
)


@dataclass(frozen=True)
class SymbolPinFigure:
    """Physical figure backing a symbol pin (typically ``rect|pin:drawing`` or inst master)."""

    kind: PinFigureKind = "rect"
    layer: str = "pin"
    purpose: str = "drawing"
    bbox: BBox | None = None
    lib: str | None = None
    cell: str | None = None
    view: str | None = None
    points: tuple[Point2D, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "rectangle":  # type: ignore[comparison-overlap]
            object.__setattr__(self, "kind", "rect")


@dataclass(frozen=True)
class SymbolPin:
    """Electrical symbol terminal contract paired with its pin location and figure."""

    name: str
    direction: str = "inputOutput"
    xy: Point2D = (0.0, 0.0)
    orient: str = "R0"
    pin_figure: SymbolPinFigure = field(default_factory=SymbolPinFigure)


@dataclass(frozen=True)
class SymbolShape:
    """Visual symbol drawing primitive (body geometry, distinct from pins & selection_box)."""

    kind: SymbolShapeInputKind
    layer: str
    purpose: str = "drawing"
    points: tuple[Point2D, ...] = ()
    width: float = 0.0
    fill: bool = False
    bbox: BBox | None = None
    arc_bbox: BBox | None = None
    ellipse_bbox: BBox | None = None

    def __post_init__(self) -> None:
        if self.kind == "rectangle":
            object.__setattr__(self, "kind", "rect")


@dataclass(frozen=True)
class SymbolLabel:
    """Symbol text label preserving exact Cadence ``labelType`` classification."""

    text: str
    xy: Point2D
    layer: str = "pin"
    purpose: str = "label"
    justify: str = "centerCenter"
    orient: str = "R0"
    font: str = "stick"
    height: float = 0.0625
    label_type: SymbolLabelType = "normalLabel"


@dataclass(frozen=True)
class SymbolGraphics:
    """Purely visual symbol body geometry, classified labels, selection box, and origin."""

    shapes: tuple[SymbolShape, ...] = ()
    labels: tuple[SymbolLabel, ...] = ()
    nlp_labels: tuple[SymbolLabel, ...] = ()
    il_labels: tuple[SymbolLabel, ...] = ()
    selection_box: BBox | None = None
    origin: Point2D = (0.0, 0.0)
    bbox: BBox | None = None


@dataclass(frozen=True)
class SymbolSnapshot:
    """Complete canonical snapshot of a Cadence OpenAccess ``schematicSymbol`` CellView."""

    manifest: Manifest
    cellview: CellViewRef
    pins: tuple[SymbolPin, ...] = ()
    port_order: tuple[str, ...] = ()
    graphics: SymbolGraphics = field(default_factory=SymbolGraphics)
    properties: dict[str, Any] = field(default_factory=dict)
    omissions: tuple[OmissionRecord, ...] = ()


__all__ = [
    "SYMBOL_SCHEMA_V1",
    "Point2D",
    "BBox",
    "SymbolPropertyScalar",
    "SymbolPropertyValue",
    "PinFigureKind",
    "SymbolShapeKind",
    "SymbolShapeInputKind",
    "SymbolLabelType",
    "VALID_PIN_FIGURE_KINDS",
    "VALID_SYMBOL_SHAPE_KINDS",
    "VALID_SYMBOL_LABEL_TYPES",
    "SymbolPinFigure",
    "SymbolPin",
    "SymbolShape",
    "SymbolLabel",
    "SymbolGraphics",
    "SymbolSnapshot",
]
