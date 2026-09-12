"""
Interface language, picked from the Windows UI language.

French and English only. Anything else falls back to English. Set the
AIRPODS_LEVELS_LANG environment variable to "fr" or "en" to force one.
"""

import ctypes
import locale
import os

_LANG = None

STRINGS = {
    # -- tray -----------------------------------------------------------
    "app.name": ("AirPods Sound Levels", "Niveau sonore AirPods"),
    "menu.exposure": ("Sound Exposure", "Exposition sonore"),
    "menu.live_backdrop": ("Live backdrop", "Fond dynamique"),
    "menu.startup": ("Open at login", "Lancer au démarrage"),
    "menu.quit": ("Quit", "Quitter"),
    "tray.unavailable": ("Measurement unavailable", "Mesure indisponible"),
    "tray.output": ("Audio output", "Sortie audio"),

    # -- level tiers ----------------------------------------------------
    "level.ok": ("OK", "OK"),
    "level.loud": ("Loud", "Bruyant"),
    "level.veryloud": ("Very loud", "Très fort"),
    "level.silent": ("Silent", "Silence"),

    # -- panel ----------------------------------------------------------
    "panel.headphone_volume": ("Headphone volume", "Volume des écouteurs"),
    "panel.no_value": ("-- dB", "-- dB"),

    # -- exposure window ------------------------------------------------
    "window.recent_days": ("RECENT DAYS", "DERNIERS JOURS"),
    "window.today": ("Today", "Aujourd'hui"),
    "window.yesterday": ("Yesterday", "Hier"),
    "window.recording": ("Recording", "Enregistrement actif"),
    "window.daily_dose": ("of the recommended daily sound dose",
                          "de la dose sonore conseillée sur une journée"),
    "window.dose_safe": ("No risk", "Sans risque"),
    "window.dose_watch": ("Worth watching", "À surveiller"),
    "window.dose_over": ("Limit exceeded", "Limite dépassée"),
    "window.dose_none": ("No exposure", "Aucune exposition"),
    "window.chart_title": ("LEVEL THROUGH THE DAY", "NIVEAU AU FIL DU JOUR"),
    "window.empty": ("Levels will appear here once you start listening",
                     "Les niveaux s'afficheront ici dès que tu écouteras "
                     "quelque chose"),

    "tile.average": ("AVERAGE", "MOYENNE"),
    "tile.average_note": ("over the day", "sur la journée"),
    "tile.peak": ("PEAK", "PIC"),
    "tile.peak_note": ("loudest level", "niveau le plus fort"),
    "tile.listening": ("LISTENING", "ÉCOUTE"),
    "tile.listening_note": ("total time", "durée totale"),
    "tile.over80": ("ABOVE 80", "AU-DESSUS DE 80"),
    "tile.over80_note": ("loud range", "zone bruyante"),
    "tile.over95": ("ABOVE 95", "AU-DESSUS DE 95"),
    "tile.over95_note": ("risk range", "zone à risque"),

    # -- console meter ---------------------------------------------------
    "cli.device": ("Device", "Périphérique"),
    "cli.current": ("CURRENT LEVEL", "NIVEAU ACTUEL"),
    "cli.avg10": ("10 s average", "Moyenne 10 s"),
    "cli.avg_session": ("Session average", "Moyenne session"),
    "cli.peak_session": ("Session peak", "Pic session"),
    "cli.volume": ("System volume", "Volume Windows"),
    "cli.signal": ("Audio signal", "Signal audio"),
    "cli.safe_time": ("Safe listening", "Écoute sûre"),
    "cli.elapsed": ("Listening for", "Écoute en cours"),
    "cli.muted": ("MUTED", "MUET"),
    "cli.unlimited": ("unlimited", "illimité"),
    "cli.stopped": ("Stopped.", "Arrêté."),
    "cli.model_unknown": ("model not in the table; using a family estimate",
                          "modèle absent de la table, estimation par famille"),
}

DAYS = (
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
     "Sunday"),
    ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"),
)

DAYS_SHORT = (
    ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    ("lun", "mar", "mer", "jeu", "ven", "sam", "dim"),
)

MONTHS = (
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"),
    ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
     "août", "septembre", "octobre", "novembre", "décembre"),
)


def language():
    """"en" or "fr"."""
    global _LANG
    if _LANG is not None:
        return _LANG

    forced = os.environ.get("AIRPODS_LEVELS_LANG", "").strip().lower()
    if forced in ("en", "fr"):
        _LANG = forced
        return _LANG

    tag = ""
    try:
        # the UI language, which is what the user actually reads; the locale
        # only says how numbers and dates are formatted
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        buf = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.LCIDToLocaleName(lcid, buf, 85, 0):
            tag = buf.value
    except Exception:
        pass
    if not tag:
        try:
            tag = locale.getdefaultlocale()[0] or ""
        except Exception:
            tag = ""

    _LANG = "fr" if tag.lower().startswith("fr") else "en"
    return _LANG


def _index():
    return 1 if language() == "fr" else 0


def t(key):
    entry = STRINGS.get(key)
    if entry is None:
        return key
    return entry[_index()]


def day_name(weekday):
    return DAYS[_index()][weekday]


def day_short(weekday):
    return DAYS_SHORT[_index()][weekday]


def month_name(month):
    return MONTHS[_index()][month - 1]


def full_date(date):
    """'Saturday 12 September' / 'Samedi 12 septembre'."""
    if language() == "fr":
        return "{} {} {}".format(day_name(date.weekday()), date.day,
                                 month_name(date.month))
    return "{} {} {}".format(day_name(date.weekday()),
                             month_name(date.month), date.day)


def short_date(date):
    """'Mar 8' / 'Mardi 8' - what fits in a sidebar row."""
    if language() == "fr":
        return "{} {}".format(day_name(date.weekday()), date.day)
    return "{} {}".format(month_name(date.month)[:3], date.day)


def percent(value):
    """French puts a narrow no-break space before the sign; English does not."""
    if language() == "fr":
        return "{:.0f} %".format(value)
    return "{:.0f}%".format(value)


def duration(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "0 min"
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    unit_h = "h"
    if hours and minutes:
        return "{} {} {:02d}".format(hours, unit_h, minutes)
    if hours:
        return "{} {}".format(hours, unit_h)
    if minutes:
        return "{} min".format(minutes)
    return "{} s".format(seconds)
