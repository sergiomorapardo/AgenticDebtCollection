"""Controles locales de contacto; no constituyen una certificación de cumplimiento legal."""

from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

COLOMBIA = ZoneInfo("America/Bogota")
SIMULATION_CHANNEL = "phone_simulation"


@lru_cache(maxsize=16)
def _holiday_dates(year: int) -> frozenset[date]:
    return frozenset(holidays.Colombia(years=year))


def check_contact(
    *, now: datetime, authorized: bool, prior_contacts: list[dict]
) -> tuple[bool, str]:
    """Historial: timestamp con zona, channel y established (contacto directo efectivo)."""
    if authorized is not True:
        return False, "El canal no tiene autorización para contacto."
    if now.tzinfo is None or now.utcoffset() is None:
        return False, "El reloj requiere zona horaria explícita."
    local_now = now.astimezone(COLOMBIA)
    local_date = local_now.date()
    if local_now.weekday() == 6 or local_date in _holiday_dates(local_date.year):
        return False, "No se permite contacto en domingos o festivos de Colombia."
    start, end = (time(8), time(15)) if local_now.weekday() == 5 else (time(7), time(19))
    if not start <= local_now.time() < end:
        return False, "El contacto está fuera del horario permitido."
    for entry in prior_contacts:
        if not isinstance(entry, dict) or type(entry.get("established")) is not bool:
            return False, "El historial de contacto está incompleto."
        if not entry["established"]:
            continue
        try:
            contacted_at = entry["timestamp"]
            if isinstance(contacted_at, str):
                contacted_at = datetime.fromisoformat(contacted_at)
            if (
                not isinstance(contacted_at, datetime)
                or contacted_at.tzinfo is None
                or contacted_at.utcoffset() is None
                or not isinstance(entry.get("channel"), str)
                or not entry["channel"]
            ):
                raise ValueError
            contacted_at = contacted_at.astimezone(COLOMBIA)
            if contacted_at > local_now:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return False, "El historial de contacto no es válido."
        if contacted_at.date() == local_date:
            return False, "Ya existe un contacto directo durante este día."
        if (
            contacted_at.date().isocalendar()[:2] == local_date.isocalendar()[:2]
            and entry["channel"] != SIMULATION_CHANNEL
        ):
            return False, "Ya se utilizó otro canal de contacto durante esta semana."
    return True, "Contacto permitido por los controles locales de simulación."
