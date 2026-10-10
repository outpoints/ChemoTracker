"""Temperature units. Readings are stored in °F; people see and type their own unit."""

from __future__ import annotations

UNITS = {"C": "°C", "F": "°F"}
DEFAULT_FEVER_F = 100.4  # 38.0 °C


def norm(unit: str | None) -> str:
    return "C" if (unit or "").upper().startswith("C") else "F"


def to_f(value: float, unit: str) -> float:
    return value * 9 / 5 + 32 if norm(unit) == "C" else value


def from_f(value_f: float, unit: str) -> float:
    return (value_f - 32) * 5 / 9 if norm(unit) == "C" else value_f


def show(value_f: float | None, unit: str, *, symbol: bool = True) -> str:
    if value_f is None:
        return ""
    number = f"{round(from_f(value_f, unit), 1):.1f}"
    return f"{number}\u00a0{UNITS[norm(unit)]}" if symbol else number


def fever_line(prefs: dict) -> float:
    """The fever line in °F, whatever unit it is shown in."""
    try:
        return float(prefs.get("fever_f") or DEFAULT_FEVER_F)
    except ValueError:
        return DEFAULT_FEVER_F


class BadTemperature(ValueError):
    pass


def parse(raw: str | None, unit: str) -> tuple[float, str | None]:
    """(°F value, note) from what someone typed. Readings plainly in the other unit are converted."""
    text = (raw or "").strip().replace(",", ".").replace("°", "").strip()
    typed_unit = None
    if text[-1:].upper() in ("C", "F"):
        typed_unit, text = text[-1].upper(), text[:-1].strip()
    try:
        value = float(text)
    except ValueError:
        raise BadTemperature("Enter the temperature as a number, for example 37.5 or 99.5.") from None
    unit = norm(unit)
    if typed_unit is None:
        if 34 <= value <= 43.5:
            typed_unit = "C"
        elif 90 <= value <= 110:
            typed_unit = "F"
        else:
            typed_unit = unit
    value_f = round(to_f(value, typed_unit), 1)
    if not 93 <= value_f <= 110:
        raise BadTemperature("That doesn't look like a body temperature. Check the thermometer, for example 37.5 °C or 99.5 °F.")
    note = f"Entered as {value:g} {UNITS[typed_unit]}." if typed_unit != unit else None
    return value_f, note


def parse_fever_line(raw: str | None, unit: str) -> float:
    try:
        value = float((raw or "").replace(",", ".").strip())
    except ValueError:
        raise BadTemperature("The fever line needs to be a number.") from None
    value_f = round(to_f(value, unit if not 90 <= value <= 110 else "F"), 2)
    if not 97 <= value_f <= 104:
        low, high = show(97, unit), show(104, unit)
        raise BadTemperature(f"The fever line should be between {low} and {high}.")
    return value_f
