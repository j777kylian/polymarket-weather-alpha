"""Temperature unit conversion with source retention."""

from __future__ import annotations

from dataclasses import dataclass


class UnitError(ValueError):
    """Raised when a temperature unit cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class Temperature:
    source_value: float
    source_unit: str
    celsius: float


def fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32.0) * 5.0 / 9.0


def kelvin_to_celsius(value: float) -> float:
    return float(value) - 273.15


def normalize_unit(unit: str) -> str:
    text = unit.strip().replace("°", "").replace(" ", "").upper()
    aliases = {
        "C": "C",
        "CELSIUS": "C",
        "DEGC": "C",
        "F": "F",
        "FAHRENHEIT": "F",
        "DEGF": "F",
        "K": "K",
        "KELVIN": "K",
    }
    if text not in aliases:
        raise UnitError(f"unsupported temperature unit: {unit!r}")
    return aliases[text]


def normalize_temperature(value: float, unit: str) -> Temperature:
    canonical = normalize_unit(unit)
    source_value = float(value)
    if canonical == "C":
        celsius = source_value
    elif canonical == "F":
        celsius = fahrenheit_to_celsius(source_value)
    else:
        celsius = kelvin_to_celsius(source_value)
    return Temperature(source_value=source_value, source_unit=unit, celsius=celsius)
