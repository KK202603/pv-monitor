"""
PV Monitor Backend – FastAPI + SQLite3 + aiohttp
Fragt Shelly-Wechselrichter per Shelly Cloud API ab und speichert Messwerte in SQLite.
Stellt REST-Endpunkte für das Frontend bereit.
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Optional

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging-Konfiguration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pv_monitor")

# ---------------------------------------------------------------------------
# Konfiguration aus Umgebungsvariablen
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    """Liest eine Umgebungsvariable und gibt den Standardwert zurück."""
    return os.environ.get(key, default)


# Shelly Cloud Credentials
SHELLY_AUTH_KEY: str = _env("SHELLY_AUTH_KEY", "")
SHELLY_CLOUD_SERVER: str = _env("SHELLY_CLOUD_SERVER", "https://shelly-46-eu.shelly.cloud")

# Gerätekonfiguration aus ENV aufbauen
DEVICES: list[dict[str, Any]] = []
for _i in range(1, 10):
    _cloud_id = _env(f"SHELLY_{_i}_ID")
    if not _cloud_id:
        break
    DEVICES.append(
        {
            "id": _i,
            "cloud_id": _cloud_id,
            "name": _env(f"SHELLY_{_i}_NAME", f"Gerät {_i}"),
            "inbetriebnahme": _env(f"SHELLY_{_i}_INBETRIEBNAHME", str(date.today())),
        }
    )

DB_PATH: str = _env("DB_PATH", "/app/data/pv.db")
POLL_INTERVAL: int = int(_env("POLL_INTERVAL", "60"))

logger.info("Konfiguration geladen: %d Gerät(e)", len(DEVICES))

# ---------------------------------------------------------------------------
# Datenbankzugriff (SQLite3 stdlib, thread-safe via check_same_thread=False)
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Öffnet eine SQLite-Verbindung mit Row-Factory für dict-Zugriff."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Erstellt das Datenbankschema falls noch nicht vorhanden."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        # Messwerte-Tabelle
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                ts        TEXT    NOT NULL,
                power_w   REAL    DEFAULT 0,
                total_wh  REAL    DEFAULT 0,
                online    INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_ts ON readings(device_id, ts)
        """)

        # Investitions-Tabelle
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id    INTEGER NOT NULL,
                betrag       REAL    NOT NULL,
                datum        TEXT    NOT NULL,
                beschreibung TEXT    DEFAULT '',
                created_at   TEXT    NOT NULL
            )
        """)

        # Strompreise-Tabelle (mit Zeitraum von/bis)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strompreise (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                preis_cent   REAL    NOT NULL,
                gueltig_ab   TEXT    NOT NULL,
                gueltig_bis  TEXT,
                beschreibung TEXT    DEFAULT '',
                created_at   TEXT    NOT NULL
            )
        """)

        # Default-Eintrag beim ersten Start (falls Tabelle leer)
        conn.execute("""
            INSERT INTO strompreise (preis_cent, gueltig_ab, gueltig_bis, beschreibung, created_at)
            SELECT 28.0, '2024-01-01', NULL, 'Standardpreis', datetime('now')
            WHERE NOT EXISTS (SELECT 1 FROM strompreise)
        """)

        conn.commit()
    logger.info("Datenbank initialisiert: %s", DB_PATH)


def _get_strompreis(conn: sqlite3.Connection, datum: str = None) -> float:
    """Gibt den Strompreis in Cent für ein bestimmtes Datum zurück.
    Ohne Datum: aktuell gültiger Preis (gueltig_bis IS NULL).
    """
    if datum is None:
        row = conn.execute(
            "SELECT preis_cent FROM strompreise WHERE gueltig_bis IS NULL ORDER BY gueltig_ab DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT preis_cent FROM strompreise
               WHERE gueltig_ab <= ? AND (gueltig_bis IS NULL OR gueltig_bis >= ?)
               ORDER BY gueltig_ab DESC LIMIT 1""",
            (datum, datum)
        ).fetchone()
    return float(row[0]) if row else 28.0


def insert_reading(
    device_id: int,
    power_w: float,
    total_wh: Optional[float],
    online: bool,
) -> None:
    """Speichert einen Messwert in der Datenbank.

    Bei online=False wird total_wh als NULL gespeichert,
    damit kein falscher Energiewert eingetragen wird.
    """
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO readings (device_id, ts, power_w, total_wh, online) VALUES (?, ?, ?, ?, ?)",
            (device_id, ts, power_w, total_wh, int(online)),
        )
        conn.commit()

# ---------------------------------------------------------------------------
# Shelly Cloud API-Abfrage
# ---------------------------------------------------------------------------

async def fetch_shelly_cloud(session: aiohttp.ClientSession, auth_key: str, server: str, device_id: str) -> tuple[float, float]:
    """Fragt Shelly Cloud API ab für ein Gerät."""
    url = f"{server}/device/status"
    data = {"auth_key": auth_key, "id": device_id}
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        result = await resp.json(content_type=None)

    if not result.get("isok"):
        raise ValueError(f"Shelly Cloud API Fehler: {result}")

    device_data = result.get("data", {})
    online = device_data.get("online", False)  # online steht auf data-Ebene, nicht in device_status
    device_status = device_data.get("device_status", {})

    if not online:
        raise ConnectionError("Gerät offline laut Cloud")

    # Energie aus verschiedenen Shelly-Generationen:
    # Gen1:      meters[] → power, total
    # Gen2 EM:   emeters[] → power, total
    # Gen2 Pro:  switch:0 → apower, aenergy.total  (z.B. Shelly Pro 3EM)
    meters = device_status.get("meters", [])
    emeters = device_status.get("emeters", [])
    switch0 = device_status.get("switch:0", {})

    if emeters:  # Gen2 Energie-Meter
        power_w = float(emeters[0].get("power", 0))
        total_wh = float(emeters[0].get("total", 0))
    elif meters:  # Gen1
        power_w = float(meters[0].get("power", 0))
        total_wh = float(meters[0].get("total", 0))
    elif switch0:  # Shelly Pro (switch:0) — apower negativ = Einspeisung
        power_w = abs(float(switch0.get("apower", 0)))
        total_wh = float(switch0.get("aenergy", {}).get("total", 0))
    else:
        # Letzter Fallback: apower direkt auf device_status-Ebene
        power_w = abs(float(device_status.get("apower", 0)))
        total_wh = float(device_status.get("aenergy", {}).get("total", 0))

    return power_w, total_wh


async def poll_device(session: aiohttp.ClientSession, device: dict[str, Any]) -> None:
    """Fragt ein einzelnes Shelly-Gerät per Cloud API ab und speichert den Messwert.

    Bei Verbindungsfehlern wird online=False und total_wh=None gespeichert.
    """
    device_id = device["id"]
    cloud_id = device["cloud_id"]
    name = device["name"]
    try:
        power_w, total_wh = await fetch_shelly_cloud(session, SHELLY_AUTH_KEY, SHELLY_CLOUD_SERVER, cloud_id)
        insert_reading(device_id, power_w, total_wh, online=True)
        logger.debug("Gerät %s (%s): %.1f W, %.1f Wh", name, cloud_id, power_w, total_wh)
    except asyncio.TimeoutError:
        logger.warning("Timeout beim Abrufen von Gerät %s (%s)", name, cloud_id)
        insert_reading(device_id, 0.0, None, online=False)
    except aiohttp.ClientError as exc:
        logger.warning("Verbindungsfehler Gerät %s (%s): %s", name, cloud_id, exc)
        insert_reading(device_id, 0.0, None, online=False)
    except Exception as exc:
        logger.error("Unerwarteter Fehler Gerät %s (%s): %s", name, cloud_id, exc)
        insert_reading(device_id, 0.0, None, online=False)


async def polling_loop() -> None:
    """Hauptschleife: Fragt alle konfigurierten Geräte im festgelegten Intervall ab."""
    logger.info("Polling-Loop gestartet (Intervall: %ds)", POLL_INTERVAL)
    async with aiohttp.ClientSession() as session:
        while True:
            tasks = [poll_device(session, dev) for dev in DEVICES]
            if tasks:
                await asyncio.gather(*tasks)
            else:
                logger.warning("Keine Geräte konfiguriert – Polling übersprungen")
            await asyncio.sleep(POLL_INTERVAL)

# ---------------------------------------------------------------------------
# FastAPI Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan-Handler: Initialisiert DB und startet Polling."""
    init_db()
    task = asyncio.create_task(polling_loop())
    logger.info("Anwendung gestartet")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Anwendung beendet")


app = FastAPI(
    title="PV Monitor API",
    description="REST-API für den PV-Monitor (Shelly-Wechselrichter via Cloud)",
    version="1.2.0",
    lifespan=lifespan,
)

# CORS für alle Methoden – in Produktion übernimmt Caddy das Routing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic-Modelle für Request-Bodies
# ---------------------------------------------------------------------------

class StrompreisBody(BaseModel):
    """Neuer Strompreis-Eintrag mit Zeitraum."""
    preis_cent: float
    gueltig_ab: str
    gueltig_bis: Optional[str] = None
    beschreibung: str = ""


class InvestmentBody(BaseModel):
    """Neue Investition."""
    device_id: int
    betrag: float
    datum: str
    beschreibung: str = ""

# ---------------------------------------------------------------------------
# Hilfsfunktionen für Berechnungen
# ---------------------------------------------------------------------------

def _today_energy_kwh(conn: sqlite3.Connection, device_id: int) -> float:
    """Berechnet die heute erzeugte Energie in kWh für ein Gerät.

    Formel: (MAX(total_wh) - MIN(total_wh)) / 60000 für den aktuellen Tag.
    Hinweis: Shelly EM Gen1 liefert total in Watt-Minuten (Wmin), nicht Wh.
    Berücksichtigt nur Zeilen mit gültigem total_wh (online=1).
    """
    today = date.today().isoformat()
    row = conn.execute(
        """
        SELECT (MAX(total_wh) - MIN(total_wh)) / 60000.0 AS kwh
        FROM readings
        WHERE device_id = ?
          AND DATE(ts) = ?
          AND total_wh IS NOT NULL
        """,
        (device_id, today),
    ).fetchone()
    if row and row["kwh"] is not None:
        return max(0.0, row["kwh"])
    return 0.0


def _last_reading(conn: sqlite3.Connection, device_id: int) -> Optional[sqlite3.Row]:
    """Gibt den neuesten Messwert eines Geräts zurück."""
    return conn.execute(
        "SELECT * FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()


def _total_energy_kwh(conn: sqlite3.Connection, device_id: int) -> float:
    """Berechnet die gesamte erzeugte Energie in kWh über alle Messwerte.

    Verwendet MAX(total_wh) - MIN(total_wh) global — funktioniert korrekt
    sowohl für historische Jahresstempel als auch für Live-Polling-Daten.
    """
    row = conn.execute(
        """
        SELECT MAX(total_wh) - MIN(total_wh)
        FROM readings
        WHERE device_id = ?
          AND total_wh IS NOT NULL
        """,
        (device_id,),
    ).fetchone()
    diff = row[0] if row and row[0] is not None else 0.0
    return max(0.0, diff) / 60000.0


def _total_savings_eur(conn: sqlite3.Connection, device_id: int) -> float:
    """Berechnet die Gesamtersparnis in EUR mit historischen Strompreisen.

    Für jede Preisperiode wird die in diesem Zeitraum produzierte Energie
    mit dem damals gültigen Preis multipliziert.
    """
    # Alle Strompreisperioden holen
    perioden = conn.execute("""
        SELECT preis_cent, gueltig_ab,
               COALESCE(gueltig_bis, date('now')) AS bis
        FROM strompreise
        ORDER BY gueltig_ab
    """).fetchall()

    total_eur = 0.0

    for periode in perioden:
        preis_cent = periode[0]
        von = periode[1]
        bis = periode[2]

        # Energie in dieser Periode: MAX(total_wh in Periode) - MIN(total_wh in Periode)
        row = conn.execute("""
            SELECT MAX(total_wh) - MIN(total_wh)
            FROM readings
            WHERE device_id = ?
              AND total_wh IS NOT NULL
              AND ts >= ?
              AND ts <= ?
        """, (device_id, von + " 00:00:00", bis + " 23:59:59")).fetchone()

        diff_wh = row[0] if row and row[0] is not None else 0.0
        kwh = max(0.0, diff_wh) / 60000.0
        total_eur += kwh * preis_cent / 100.0

    return round(total_eur, 2)


def _avg_daily_kwh_30d(conn: sqlite3.Connection, device_id: int) -> float:
    """Berechnet die durchschnittliche Tagesleistung der letzten 30 Tage in kWh."""
    since = (date.today() - timedelta(days=30)).isoformat()
    rows = conn.execute(
        """
        SELECT DATE(ts) AS tag,
               MAX(total_wh) - MIN(total_wh) AS diff_wh
        FROM readings
        WHERE device_id = ?
          AND DATE(ts) >= ?
          AND total_wh IS NOT NULL
        GROUP BY DATE(ts)
        HAVING diff_wh IS NOT NULL
        """,
        (device_id, since),
    ).fetchall()
    if not rows:
        return 0.0
    valid = [max(0.0, r["diff_wh"]) for r in rows if r["diff_wh"] is not None]
    if not valid:
        return 0.0
    return (sum(valid) / len(valid)) / 60000.0

# ---------------------------------------------------------------------------
# API Endpoints – Status & Charts
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status() -> dict:
    """Gibt den aktuellen Status aller Geräte zurück.

    Enthält: Name, Online-Status, aktuelle Leistung (W),
    heutige Energie (kWh), heutige Ersparnis (EUR), letzter Messzeitpunkt.
    Strompreis wird aus der strompreise-Tabelle gelesen.
    """
    result = []
    with get_db() as conn:
        strompreis_cent = _get_strompreis(conn)
        for dev in DEVICES:
            did = dev["id"]
            last = _last_reading(conn, did)
            online = bool(last and last["online"]) if last else False
            power_w = float(last["power_w"]) if last and last["power_w"] is not None else 0.0
            last_seen = last["ts"] if last else None
            today_kwh = _today_energy_kwh(conn, did)
            today_eur = round(today_kwh * strompreis_cent / 100.0, 4)
            result.append(
                {
                    "id": did,
                    "name": dev["name"],
                    "online": online,
                    "power_w": round(power_w, 1),
                    "today_kwh": round(today_kwh, 4),
                    "today_eur": today_eur,
                    "last_seen": last_seen,
                }
            )
    return {"devices": result}


@app.get("/api/chart/24h")
async def get_chart_24h() -> dict:
    """Liefert Stundenmittelwerte der Leistung der letzten 24 Stunden.

    Rückgabe: Labels (Stunden) und Datensätze pro Gerät (avg W pro Stunde).
    """
    since = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        # Alle Stunden-Labels der letzten 24h erzeugen
        hours: list[str] = []
        hour_cursor = datetime.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        for _ in range(24):
            hours.append(hour_cursor.strftime("%Y-%m-%d %H:00"))
            hour_cursor += timedelta(hours=1)

        datasets = []
        colors = ["rgba(234, 179, 8, 0.8)", "rgba(34, 197, 94, 0.8)"]
        border_colors = ["rgba(234, 179, 8, 1)", "rgba(34, 197, 94, 1)"]
        bg_colors = ["rgba(234, 179, 8, 0.15)", "rgba(34, 197, 94, 0.15)"]

        for idx, dev in enumerate(DEVICES):
            rows = conn.execute(
                """
                SELECT strftime('%Y-%m-%d %H:00', ts) AS hour,
                       AVG(power_w) AS avg_w
                FROM readings
                WHERE device_id = ?
                  AND ts >= ?
                  AND online = 1
                GROUP BY hour
                ORDER BY hour
                """,
                (dev["id"], since),
            ).fetchall()
            # Stundenwerte in ein Dict packen für schnelles Lookup
            hour_map = {r["hour"]: round(r["avg_w"], 1) for r in rows if r["avg_w"] is not None}
            data = [hour_map.get(h, None) for h in hours]
            datasets.append(
                {
                    "device_id": dev["id"],
                    "name": dev["name"],
                    "data": data,
                    "borderColor": border_colors[idx % len(border_colors)],
                    "backgroundColor": bg_colors[idx % len(bg_colors)],
                    "pointBackgroundColor": colors[idx % len(colors)],
                }
            )
    # Lesbare Stunden-Labels (z.B. "14:00")
    labels = [h[-5:] for h in hours]
    return {"labels": labels, "datasets": datasets}


@app.get("/api/chart/30days")
async def get_chart_30days() -> dict:
    """Liefert die tägliche Energieerzeugung der letzten 30 Tage in kWh.

    Rückgabe: Labels (Datum) und Datensätze pro Gerät (kWh pro Tag).
    """
    since = (date.today() - timedelta(days=29)).isoformat()
    # Alle 30 Tage als Labels vorbereiten
    day_labels: list[str] = []
    day_cursor = date.today() - timedelta(days=29)
    for _ in range(30):
        day_labels.append(day_cursor.isoformat())
        day_cursor += timedelta(days=1)

    with get_db() as conn:
        datasets = []
        colors = ["rgba(234, 179, 8, 0.7)", "rgba(34, 197, 94, 0.7)"]
        border_colors = ["rgba(234, 179, 8, 1)", "rgba(34, 197, 94, 1)"]

        for idx, dev in enumerate(DEVICES):
            rows = conn.execute(
                """
                SELECT DATE(ts) AS tag,
                       (MAX(total_wh) - MIN(total_wh)) / 60000.0 AS kwh
                FROM readings
                WHERE device_id = ?
                  AND DATE(ts) >= ?
                  AND total_wh IS NOT NULL
                GROUP BY DATE(ts)
                ORDER BY tag
                """,
                (dev["id"], since),
            ).fetchall()
            day_map = {
                r["tag"]: round(max(0.0, r["kwh"]), 3)
                for r in rows
                if r["kwh"] is not None
            }
            data = [day_map.get(d, None) for d in day_labels]
            datasets.append(
                {
                    "device_id": dev["id"],
                    "name": dev["name"],
                    "data": data,
                    "backgroundColor": colors[idx % len(colors)],
                    "borderColor": border_colors[idx % len(border_colors)],
                    "borderWidth": 1,
                }
            )
    # Lesbare Tages-Labels (DD.MM.)
    labels = [
        datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.")
        for d in day_labels
    ]
    return {"labels": labels, "datasets": datasets}

# ---------------------------------------------------------------------------
# API Endpoints – Amortisation & Projektion
# ---------------------------------------------------------------------------

@app.get("/api/amortisation")
async def get_amortisation() -> list:
    """Berechnet den Amortisationsstand jeder Anlage.

    Kosten werden aus der investments-Tabelle summiert.
    Strompreis: aktuell gültiger Preis für Gesamtersparnis.
    """
    result = []
    today = date.today()
    with get_db() as conn:
        strompreis_cent = _get_strompreis(conn)
        for dev in DEVICES:
            did = dev["id"]
            inbetriebnahme = datetime.strptime(dev["inbetriebnahme"], "%Y-%m-%d").date()
            days_since = max(1, (today - inbetriebnahme).days)

            # Kosten aus investments-Tabelle summieren
            row = conn.execute(
                "SELECT COALESCE(SUM(betrag), 0) FROM investments WHERE device_id = ?",
                (did,),
            ).fetchone()
            kosten = float(row[0]) if row else 0.0

            total_kwh = _total_energy_kwh(conn, did)
            savings_eur = _total_savings_eur(conn, did)
            progress_pct = round(min(100.0, (savings_eur / kosten * 100.0) if kosten > 0 else 0.0), 1)

            avg_daily_kwh = total_kwh / days_since
            avg_daily_eur = avg_daily_kwh * strompreis_cent / 100.0

            breakeven_date: Optional[str] = None
            reingewinn_eur: Optional[float] = None

            if kosten <= 0:
                # Keine Investition erfasst
                breakeven_date = "–"
            elif progress_pct >= 100.0:
                reingewinn_eur = round(savings_eur - kosten, 2)
            else:
                if avg_daily_eur > 0:
                    days_to_breakeven = (kosten - savings_eur) / avg_daily_eur
                    be_date = today + timedelta(days=days_to_breakeven)
                    breakeven_date = be_date.strftime("%d.%m.%Y")
                else:
                    breakeven_date = "–"

            result.append(
                {
                    "device_id": did,
                    "name": dev["name"],
                    "kosten": round(kosten, 2),
                    "total_kwh": round(total_kwh, 2),
                    "savings_eur": savings_eur,
                    "progress_pct": progress_pct,
                    "breakeven_date": breakeven_date,
                    "reingewinn_eur": reingewinn_eur,
                    "inbetriebnahme": dev["inbetriebnahme"],
                }
            )
    return result


@app.get("/api/projection")
async def get_projection() -> list:
    """Berechnet die Jahresprojektion für jede Anlage.

    Basis: Durchschnittliche Tagesleistung der letzten 30 Tage * 365.
    Strompreis wird aus der strompreise-Tabelle gelesen.
    """
    result = []
    with get_db() as conn:
        strompreis_cent = _get_strompreis(conn)
        for dev in DEVICES:
            did = dev["id"]
            avg_daily_kwh = _avg_daily_kwh_30d(conn, did)
            projected_kwh_year = round(avg_daily_kwh * 365.0, 1)
            projected_eur_year = round(projected_kwh_year * strompreis_cent / 100.0, 2)
            result.append(
                {
                    "device_id": did,
                    "name": dev["name"],
                    "projected_kwh_year": projected_kwh_year,
                    "projected_eur_year": projected_eur_year,
                }
            )
    return result

# ---------------------------------------------------------------------------
# API Endpoints – Strompreise
# ---------------------------------------------------------------------------

@app.get("/api/strompreise")
async def get_strompreise() -> list:
    """Gibt alle Strompreis-Einträge zurück, sortiert nach gueltig_ab DESC."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, preis_cent, gueltig_ab, gueltig_bis, beschreibung FROM strompreise ORDER BY gueltig_ab DESC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "preis_cent": r["preis_cent"],
            "gueltig_ab": r["gueltig_ab"],
            "gueltig_bis": r["gueltig_bis"],
            "beschreibung": r["beschreibung"] or "",
        }
        for r in rows
    ]


@app.post("/api/strompreise")
async def add_strompreis(body: StrompreisBody) -> dict:
    """Fügt einen neuen Strompreis-Eintrag hinzu.

    Wenn gueltig_bis nicht gesetzt ist, wird der vorherige offene Eintrag
    automatisch mit gueltig_bis = gueltig_ab - 1 Tag abgeschlossen.
    """
    if body.preis_cent <= 0 or body.preis_cent > 500:
        raise HTTPException(status_code=400, detail="Preis muss zwischen 0 und 500 Cent liegen.")

    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO strompreise (preis_cent, gueltig_ab, gueltig_bis, beschreibung, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (body.preis_cent, body.gueltig_ab, body.gueltig_bis, body.beschreibung, created_at),
        )
        new_id = cursor.lastrowid

        # Wenn kein gueltig_bis → aktuellen offenen Eintrag schließen
        if not body.gueltig_bis:
            conn.execute(
                """UPDATE strompreise SET gueltig_bis = date(?, '-1 day')
                   WHERE gueltig_bis IS NULL AND id != ?""",
                (body.gueltig_ab, new_id),
            )

        conn.commit()

    logger.info(
        "Strompreis hinzugefügt: %.2f ct/kWh ab %s bis %s",
        body.preis_cent, body.gueltig_ab, body.gueltig_bis or "offen"
    )
    return {
        "id": new_id,
        "preis_cent": body.preis_cent,
        "gueltig_ab": body.gueltig_ab,
        "gueltig_bis": body.gueltig_bis,
        "beschreibung": body.beschreibung,
    }


@app.delete("/api/strompreise/{strompreis_id}")
async def delete_strompreis(strompreis_id: int) -> dict:
    """Löscht einen Strompreis-Eintrag. Verhindert Löschen des letzten Eintrags."""
    with get_db() as conn:
        # Gesamtanzahl prüfen
        count_row = conn.execute("SELECT COUNT(*) FROM strompreise").fetchone()
        if count_row and count_row[0] <= 1:
            raise HTTPException(status_code=400, detail="Der letzte Strompreis-Eintrag kann nicht gelöscht werden.")

        row = conn.execute(
            "SELECT id FROM strompreise WHERE id = ?", (strompreis_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Strompreis {strompreis_id} nicht gefunden.")

        conn.execute("DELETE FROM strompreise WHERE id = ?", (strompreis_id,))
        conn.commit()

    logger.info("Strompreis %d gelöscht", strompreis_id)
    return {"ok": True}

# ---------------------------------------------------------------------------
# API Endpoints – Investitionen
# ---------------------------------------------------------------------------

@app.get("/api/investments")
async def get_investments() -> list:
    """Gibt alle erfassten Investitionen zurück, angereichert mit Gerätename."""
    # Gerätename-Lookup aus DEVICES
    device_map = {dev["id"]: dev["name"] for dev in DEVICES}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, device_id, betrag, datum, beschreibung FROM investments ORDER BY datum DESC, id DESC"
        ).fetchall()
    result = []
    for r in rows:
        result.append(
            {
                "id": r["id"],
                "device_id": r["device_id"],
                "device_name": device_map.get(r["device_id"], f"Gerät {r['device_id']}"),
                "betrag": r["betrag"],
                "datum": r["datum"],
                "beschreibung": r["beschreibung"] or "",
            }
        )
    return result


@app.post("/api/investments")
async def add_investment(body: InvestmentBody) -> dict:
    """Fügt eine neue Investition hinzu."""
    # Geräte-ID validieren
    valid_ids = {dev["id"] for dev in DEVICES}
    if body.device_id not in valid_ids:
        raise HTTPException(status_code=400, detail=f"Unbekannte Geräte-ID: {body.device_id}")
    if body.betrag <= 0:
        raise HTTPException(status_code=400, detail="Betrag muss größer als 0 sein.")

    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO investments (device_id, betrag, datum, beschreibung, created_at) VALUES (?, ?, ?, ?, ?)",
            (body.device_id, body.betrag, body.datum, body.beschreibung, created_at),
        )
        conn.commit()
        new_id = cursor.lastrowid

    logger.info("Investition hinzugefügt: Gerät %d, %.2f EUR am %s", body.device_id, body.betrag, body.datum)
    device_map = {dev["id"]: dev["name"] for dev in DEVICES}
    return {
        "id": new_id,
        "device_id": body.device_id,
        "device_name": device_map.get(body.device_id, f"Gerät {body.device_id}"),
        "betrag": body.betrag,
        "datum": body.datum,
        "beschreibung": body.beschreibung,
    }


@app.delete("/api/investments/{investment_id}")
async def delete_investment(investment_id: int) -> dict:
    """Löscht eine Investition anhand ihrer ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM investments WHERE id = ?", (investment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Investition {investment_id} nicht gefunden.")
        conn.execute("DELETE FROM investments WHERE id = ?", (investment_id,))
        conn.commit()
    logger.info("Investition %d gelöscht", investment_id)
    return {"ok": True}

# ---------------------------------------------------------------------------
# Gesundheitsprüfung
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Gesundheitsprüfung für den Container-Orchestrator."""
    return {"status": "ok", "devices": len(DEVICES)}
