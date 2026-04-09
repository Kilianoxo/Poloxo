#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Poloxo — Bot de trading météo Polymarket
Ville : Paris (LFPG) | Scan : 15 min | Capital : 30$ | Mise max : 10$
"""

import json
import math
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Chemins
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
MARKETS_DIR = DATA_DIR / "markets"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = DATA_DIR / "state.json"
CALIB_FILE  = DATA_DIR / "calibration.json"

for _d in [DATA_DIR, MARKETS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
def charger_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

CFG = charger_config()

PRIVATE_KEY    = CFG.get("private_key", "")
BALANCE        = CFG.get("balance", 30.0)
MAX_BET        = CFG.get("max_bet", 10.0)
MIN_EV         = CFG.get("min_ev", 0.05)
MAX_PRICE      = CFG.get("max_price", 0.45)
MIN_VOLUME     = CFG.get("min_volume", 500)
MIN_HEURES     = CFG.get("min_hours", 2.0)
MAX_HEURES     = CFG.get("max_hours", 72.0)
KELLY_FRACTION = CFG.get("kelly_fraction", 0.25)
MAX_SLIPPAGE   = CFG.get("max_slippage", 0.03)
SCAN_INTERVAL  = CFG.get("scan_interval", 900)
CALIB_MIN      = CFG.get("calibration_min", 20)
DISCORD_WH     = CFG.get("discord_webhook", "")

MODE_SIMU = not PRIVATE_KEY or PRIVATE_KEY == "YOUR_PRIVATE_KEY_HERE"


# ─────────────────────────────────────────────────────────────────────────────
# Paris
# ─────────────────────────────────────────────────────────────────────────────
PARIS = {
    "slug":    "paris",
    "name":    "Paris",
    "lat":     48.9962,
    "lon":     2.5979,
    "station": "LFPG",
    "tz":      "Europe/Paris",
}

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

MOIS = {
    "january":1,  "february":2,  "march":3,     "april":4,
    "may":5,      "june":6,      "july":7,       "august":8,
    "september":9,"october":10,  "november":11,  "december":12,
    "jan":1, "feb":2, "mar":3, "apr":4, "jun":6,
    "jul":7, "aug":8, "sep":9, "oct":10,"nov":11,"dec":12,
}


# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────
ICONES = {"INFO": "·", "OK": "✓", "WARN": "!", "ERR": "✗", "TRADE": "$"}

def log(msg: str, niveau: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
    print(f"[{ts}] {ICONES.get(niveau, '·')} {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Discord webhook
# ─────────────────────────────────────────────────────────────────────────────
def discord(msg: str):
    if not DISCORD_WH or DISCORD_WH == "YOUR_DISCORD_WEBHOOK_URL":
        return
    try:
        requests.post(DISCORD_WH, json={"content": msg}, timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Sources météo
# ─────────────────────────────────────────────────────────────────────────────
def get_ecmwf(dates: list) -> dict:
    """Prévisions ECMWF quotidiennes max pour Paris (Open-Meteo, gratuit)."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={PARIS['lat']}&longitude={PARIS['lon']}"
            f"&daily=temperature_2m_max"
            f"&temperature_unit=celsius"
            f"&forecast_days=7"
            f"&timezone={PARIS['tz']}"
            f"&models=ecmwf_ifs025&bias_correction=true"
        )
        r = requests.get(url, timeout=(5, 10)).json()
        daily = r.get("daily", {})
        result = {}
        for d, t in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
            if d in dates and t is not None:
                result[d] = round(float(t), 1)
        return result
    except Exception as e:
        log(f"ECMWF erreur : {e}", "ERR")
        return {}


def get_metar() -> Optional[float]:
    """Observation temps réel LFPG (température en °C)."""
    try:
        url = (
            f"https://aviationweather.gov/api/data/metar"
            f"?ids={PARIS['station']}&format=json"
        )
        r = requests.get(url, timeout=(5, 8)).json()
        if r and r[0].get("temp") is not None:
            return float(r[0]["temp"])
    except Exception as e:
        log(f"METAR erreur : {e}", "ERR")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Polymarket — Gamma API
# ─────────────────────────────────────────────────────────────────────────────
def chercher_marches_paris() -> list:
    """Cherche les marchés température Paris actifs sur Polymarket."""
    try:
        params = {
            "q":      "Paris temperature",
            "active": "true",
            "closed": "false",
            "limit":  50,
        }
        r = requests.get(f"{GAMMA_URL}/markets", params=params, timeout=(5, 12)).json()
        marches = r if isinstance(r, list) else r.get("markets", [])

        resultats = []
        for m in marches:
            q = (m.get("question") or "").lower()
            if "paris" in q and any(
                kw in q for kw in ["temperature", "temp", "highest", "°c", "celsius"]
            ):
                resultats.append(m)
        return resultats
    except Exception as e:
        log(f"Polymarket search erreur : {e}", "ERR")
        return []


def parse_date_marche(marche: dict) -> Optional[str]:
    """Extrait la date cible (YYYY-MM-DD) depuis la question ou endDate."""
    question = marche.get("question", "")

    # Ex: "Highest temperature in Paris on March 20?"
    m = re.search(
        r"on\s+(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?",
        question,
        re.IGNORECASE,
    )
    if m:
        mois_str = m.group(1).lower()
        jour     = int(m.group(2))
        annee    = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
        num_mois = MOIS.get(mois_str)
        if num_mois:
            try:
                return datetime(annee, num_mois, jour).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Fallback : endDate − 1 jour
    end = marche.get("endDate") or marche.get("end_date_iso")
    if end:
        try:
            dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            pass

    return None


def heures_restantes(marche: dict) -> float:
    """Nombre d'heures avant clôture du marché."""
    end = marche.get("endDate") or marche.get("end_date_iso")
    if not end:
        return 999.0
    try:
        dt  = datetime.fromisoformat(end.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds() / 3600)
    except Exception:
        return 999.0


def parser_buckets(marche: dict) -> list:
    """Parse les outcomes (buckets de température) et leurs prix."""
    outcomes = marche.get("outcomes")
    prices   = marche.get("outcomePrices")
    tokens   = marche.get("tokens") or []

    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []

    buckets = []
    for i, (nom, prix) in enumerate(zip(outcomes or [], prices or [])):
        try:
            token_id = tokens[i]["token_id"] if i < len(tokens) else None
            buckets.append({
                "nom":      nom,
                "prix":     float(prix),
                "token_id": token_id,
            })
        except Exception:
            pass
    return buckets


# ─────────────────────────────────────────────────────────────────────────────
# Correspondance température → bucket
# ─────────────────────────────────────────────────────────────────────────────
def _bornes_bucket(nom: str) -> Optional[tuple]:
    """
    Retourne (lo, hi) pour un nom de bucket, ou None si non parsable.
    Gère : 'Below 5°C', '5 to 9°C', '10-14°C', '20°C or above', '20+', etc.
    """
    n = nom.lower().replace("°c", "").replace("°", "").strip()

    # "below X" / "< X" / "under X"
    m = re.match(r"(?:below|<|under)\s*([-\d.]+)", n)
    if m:
        return (-99.0, float(m.group(1)))

    # "above X" / "> X" / "over X" / "X or above" / "X+"
    m = re.match(r"(?:above|>|over)\s*([-\d.]+)", n)
    if m:
        return (float(m.group(1)), 99.0)
    m = re.match(r"([-\d.]+)\s+or\s+above", n)
    if m:
        return (float(m.group(1)), 99.0)
    m = re.match(r"([-\d.]+)\+", n)
    if m:
        return (float(m.group(1)), 99.0)

    # "X to Y" / "X-Y" / "X – Y"
    m = re.match(r"([-\d.]+)\s*(?:to|-|–)\s*([-\d.]+)", n)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    return None


def bucket_pour_temp(temp: float, buckets: list) -> Optional[dict]:
    """Trouve le bucket dans lequel tombe la température prévue."""
    for b in buckets:
        bornes = _bornes_bucket(b["nom"])
        if bornes is None:
            continue
        lo, hi = bornes
        # Borne inférieure inclusive, borne supérieure exclusive (sauf dernier)
        if lo <= temp < hi:
            return b
        # Bucket "above X" : temp >= lo
        if hi == 99.0 and temp >= lo:
            return b
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Maths : EV + Kelly
# ─────────────────────────────────────────────────────────────────────────────
def calc_ev(p: float, prix: float) -> float:
    if prix <= 0 or prix >= 1:
        return 0.0
    return round(p * (1.0 / prix - 1.0) - (1.0 - p), 4)


def calc_kelly(p: float, prix: float) -> float:
    if prix <= 0 or prix >= 1:
        return 0.0
    b = 1.0 / prix - 1.0
    f = (p * b - (1.0 - p)) / b
    return min(max(0.0, f) * KELLY_FRACTION, 1.0)


def taille_mise(kelly: float, balance: float) -> float:
    return round(min(kelly * balance, MAX_BET), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Calibration — probabilité réaliste via distribution normale
# ─────────────────────────────────────────────────────────────────────────────
def charger_calibration() -> dict:
    if CALIB_FILE.exists():
        with open(CALIB_FILE) as f:
            return json.load(f)
    return {}


def get_sigma() -> float:
    """Sigma calibré pour Paris (écart-type de l'erreur ECMWF). Défaut : ±2°C."""
    cal = charger_calibration()
    e   = cal.get("paris_ecmwf", {})
    if e.get("n", 0) >= CALIB_MIN and e.get("sigma"):
        return float(e["sigma"])
    return 2.0


def prob_bucket(temp_prevision: float, bucket: dict, sigma: float) -> float:
    """
    P(temp_réelle ∈ bucket) via distribution normale N(temp_prevision, sigma).
    Intègre la densité entre les bornes du bucket.
    """
    bornes = _bornes_bucket(bucket["nom"])
    if bornes is None:
        return 1.0

    lo, hi = bornes
    lo = max(lo, -50.0)
    hi = min(hi,  60.0)

    def norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf((x - temp_prevision) / (sigma * math.sqrt(2))))

    p = norm_cdf(hi) - norm_cdf(lo)
    return max(0.001, min(0.999, p))


def mettre_a_jour_calibration(marche: dict):
    """
    Après résolution, compare prévision ECMWF vs temp réelle
    et met à jour l'erreur absolue moyenne (MAE / sigma).
    """
    temp_reelle = marche.get("actual_temp")
    snaps       = marche.get("forecast_snapshots", [])
    if temp_reelle is None or not snaps:
        return

    # Prendre le dernier snapshot ECMWF avant résolution
    ecmwf_prev = None
    for s in reversed(snaps):
        if s.get("ecmwf") is not None:
            ecmwf_prev = s["ecmwf"]
            break
    if ecmwf_prev is None:
        return

    erreur = abs(ecmwf_prev - temp_reelle)
    cal    = charger_calibration()
    entry  = cal.get("paris_ecmwf", {"n": 0, "sum_err": 0.0, "sigma": 2.0})

    entry["n"]       += 1
    entry["sum_err"] += erreur
    entry["sigma"]    = round(entry["sum_err"] / entry["n"], 3)

    cal["paris_ecmwf"] = entry
    with open(CALIB_FILE, "w") as f:
        json.dump(cal, f, indent=2)

    log(f"Calibration mise à jour : sigma={entry['sigma']}°C (n={entry['n']})")


# ─────────────────────────────────────────────────────────────────────────────
# Data storage — un JSON par marché
# ─────────────────────────────────────────────────────────────────────────────
def chemin_marche(date_str: str) -> Path:
    return MARKETS_DIR / f"paris_{date_str}.json"


def charger_marche(date_str: str) -> Optional[dict]:
    p = chemin_marche(date_str)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


def sauver_marche(mkt: dict):
    with open(chemin_marche(mkt["date"]), "w") as f:
        json.dump(mkt, f, indent=2, ensure_ascii=False)


def nouveau_marche(date_str: str, market_id: str, question: str) -> dict:
    return {
        "ville":               "paris",
        "date":                date_str,
        "market_id":           market_id,
        "question":            question,
        "statut":              "ouvert",
        "position":            None,
        "temp_reelle":         None,
        "outcome_resolu":      None,
        "pnl":                 None,
        "forecast_snapshots":  [],
        "marche_snapshots":    [],
        "cree_le":             datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# État global (balance, statistiques)
# ─────────────────────────────────────────────────────────────────────────────
def charger_etat() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"balance": BALANCE, "trades": 0, "victoires": 0, "defaites": 0}


def sauver_etat(etat: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(etat, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# CLOB — Placement d'ordre réel
# ─────────────────────────────────────────────────────────────────────────────
def placer_ordre(token_id: str, prix: float, montant_usdc: float) -> Optional[str]:
    """
    Place un ordre BUY sur le CLOB Polymarket.
    En mode simulation (pas de clé privée), retourne un ID fictif.
    """
    if MODE_SIMU:
        order_id = f"SIMU_{int(time.time())}"
        log(f"[SIMULATION] Ordre fictif {order_id} | {montant_usdc}$ @ {prix:.3f}", "TRADE")
        return order_id

    if not token_id:
        log("token_id manquant — impossible de placer l'ordre", "ERR")
        return None

    try:
        from py_clob_client.client import ClobClient          # type: ignore
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore

        client = ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=137)
        creds  = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        nb_shares = round(montant_usdc / prix, 4)
        order_args = OrderArgs(token_id=token_id, price=prix, size=nb_shares, side="BUY")
        signed     = client.create_order(order_args)
        resp       = client.post_order(signed, OrderType.GTC)

        order_id = resp.get("orderID") or resp.get("id") or resp.get("orderId")
        log(f"Ordre placé : {order_id} | {montant_usdc}$ @ {prix:.3f}", "TRADE")
        return order_id

    except ImportError:
        log("py-clob-client absent. Lance : pip install py-clob-client", "ERR")
        return None
    except Exception as e:
        log(f"CLOB erreur : {e}", "ERR")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Auto-résolution via Polymarket API
# ─────────────────────────────────────────────────────────────────────────────
def tenter_resolution(mkt: dict) -> bool:
    """
    Vérifie si le marché est clôturé et enregistre le résultat.
    Retourne True si résolu.
    """
    mid = mkt.get("market_id")
    if not mid:
        return False

    try:
        r = requests.get(f"{GAMMA_URL}/markets/{mid}", timeout=(5, 10)).json()
        if not r.get("closed"):
            return False

        # Trouver l'outcome gagnant (prix ≥ 0.95)
        outcomes = r.get("outcomes") or []
        prices   = r.get("outcomePrices") or []
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)

        gagnant = None
        for outcome, prix in zip(outcomes, prices):
            try:
                if float(prix) >= 0.95:
                    gagnant = outcome
                    break
            except Exception:
                pass

        mkt["statut"]          = "résolu"
        mkt["outcome_resolu"]  = gagnant

        pos = mkt.get("position")
        if pos and gagnant:
            if pos["bucket"] == gagnant:
                pnl = round(pos["montant"] * (1.0 / pos["prix"] - 1.0), 2)
                mkt["pnl"] = pnl
                log(f"Paris {mkt['date']} GAGNÉ +{pnl}$ ({gagnant})", "OK")
                discord(
                    f"✅ **GAGNÉ** | Paris {mkt['date']}\n"
                    f"Bucket : {gagnant} | +{pnl}$"
                )
            else:
                pnl = -pos["montant"]
                mkt["pnl"] = pnl
                log(f"Paris {mkt['date']} PERDU {pnl}$ | Prédit : {pos['bucket']} | Réel : {gagnant}", "WARN")
                discord(
                    f"❌ **PERDU** | Paris {mkt['date']}\n"
                    f"Prédit : {pos['bucket']} | Réel : {gagnant} | {pnl}$"
                )

        mettre_a_jour_calibration(mkt)
        return True

    except Exception as e:
        log(f"Résolution erreur ({mid}) : {e}", "ERR")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Traitement d'un marché individuel
# ─────────────────────────────────────────────────────────────────────────────
def traiter_marche(marche: dict, ecmwf: dict, metar_temp: Optional[float], etat: dict):
    mid      = marche.get("id") or marche.get("conditionId") or ""
    question = marche.get("question", "")

    date_str = parse_date_marche(marche)
    if not date_str:
        return

    heures = heures_restantes(marche)
    if heures < MIN_HEURES or heures > MAX_HEURES:
        return

    volume = float(marche.get("volume") or 0)
    if volume < MIN_VOLUME:
        log(f"Paris {date_str} : skip (volume {volume:.0f}$ < {MIN_VOLUME}$)")
        return

    mkt = charger_marche(date_str) or nouveau_marche(date_str, mid, question)

    # Déjà positionné sur ce marché
    if mkt.get("position"):
        return

    # Prévision ECMWF pour cette date
    prevision = ecmwf.get(date_str)
    if prevision is None:
        sauver_marche(mkt)
        return

    # Snapshot forecast
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mkt["forecast_snapshots"].append({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "heures":  round(heures, 1),
        "ecmwf":   prevision,
        "metar":   metar_temp if date_str == today_str else None,
    })

    buckets = parser_buckets(marche)
    if not buckets:
        sauver_marche(mkt)
        return

    # Snapshot marché
    mkt["marche_snapshots"].append({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "buckets": [{"nom": b["nom"], "prix": b["prix"]} for b in buckets],
        "volume":  volume,
        "heures":  round(heures, 1),
    })

    # Bucket cible
    cible = bucket_pour_temp(prevision, buckets)
    if not cible:
        log(f"Paris {date_str} : pas de bucket pour {prevision}°C", "WARN")
        sauver_marche(mkt)
        return

    prix_mid = cible["prix"]
    if prix_mid > MAX_PRICE:
        log(f"Paris {date_str} : skip {cible['nom']} prix={prix_mid:.3f} > max={MAX_PRICE}")
        sauver_marche(mkt)
        return

    # Prix d'entrée = ask (mid + slippage)
    ask = min(prix_mid + MAX_SLIPPAGE, 0.99)

    # Probabilité (distribution normale)
    sigma = get_sigma()
    p     = prob_bucket(prevision, cible, sigma)

    # EV
    ev = calc_ev(p, ask)
    log(
        f"Paris {date_str} : prév={prevision}°C → {cible['nom']} "
        f"| prix={prix_mid:.3f} | p={p:.0%} | EV={ev:.1%} | σ={sigma}°C"
    )

    if ev < MIN_EV:
        sauver_marche(mkt)
        return

    # Kelly sizing
    k      = calc_kelly(p, ask)
    montant = taille_mise(k, etat["balance"])
    if montant < 0.50:
        sauver_marche(mkt)
        return

    # Placement ordre
    order_id = placer_ordre(cible.get("token_id"), ask, montant)
    if not order_id:
        sauver_marche(mkt)
        return

    mkt["position"] = {
        "bucket":    cible["nom"],
        "token_id":  cible.get("token_id"),
        "prix":      ask,
        "montant":   montant,
        "order_id":  order_id,
        "ouvert_le": datetime.now(timezone.utc).isoformat(),
        "ev":        ev,
        "p":         round(p, 3),
        "sigma":     sigma,
    }
    mkt["statut"] = "positionné"
    etat["balance"] = round(etat["balance"] - montant, 2)

    log(
        f"POSITION ouverte | Paris {date_str} | {cible['nom']} "
        f"@ {ask:.3f} | {montant}$ | EV={ev:.1%}",
        "TRADE",
    )
    discord(
        f"💰 **POSITION** | Paris {date_str}\n"
        f"Bucket : {cible['nom']} | Prix : {ask:.3f} | Mise : {montant}$\n"
        f"Prévision : {prevision}°C | p={p:.0%} | EV={ev:.1%}"
    )

    sauver_marche(mkt)


# ─────────────────────────────────────────────────────────────────────────────
# Scan principal
# ─────────────────────────────────────────────────────────────────────────────
def scan():
    log("─── Scan démarré ───")
    etat = charger_etat()

    # 1. Prévisions météo
    aujourd_hui = datetime.now(timezone.utc)
    dates = [(aujourd_hui + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    ecmwf      = get_ecmwf(dates)
    metar_temp = get_metar()

    if not ecmwf:
        log("Aucune donnée ECMWF disponible", "ERR")
        return

    log(f"ECMWF : {ecmwf}")
    log(f"METAR LFPG : {metar_temp}°C" if metar_temp else "METAR : indisponible")

    # 2. Marchés Paris actifs
    marches = chercher_marches_paris()
    log(f"{len(marches)} marché(s) Paris trouvé(s)")

    for marche in marches:
        try:
            traiter_marche(marche, ecmwf, metar_temp, etat)
        except Exception as e:
            log(f"Erreur marché {marche.get('id', '?')} : {e}", "ERR")
            traceback.print_exc()

    # 3. Résolution automatique des positions ouvertes
    for fichier in sorted(MARKETS_DIR.glob("paris_*.json")):
        with open(fichier) as f:
            mkt = json.load(f)

        if mkt["statut"] == "positionné" and mkt.get("position"):
            if tenter_resolution(mkt):
                if mkt.get("pnl") is not None:
                    etat["balance"]  = round(etat["balance"] + mkt["pnl"] + mkt["position"]["montant"], 2)
                    etat["trades"]  += 1
                    if mkt["pnl"] > 0:
                        etat["victoires"] += 1
                    else:
                        etat["defaites"] += 1
                sauver_marche(mkt)

    sauver_etat(etat)

    wr = (
        f"{etat['victoires']}/{etat['trades']}"
        if etat["trades"] > 0 else "0/0"
    )
    log(f"Balance : {etat['balance']}$ | Trades : {etat['trades']} | V/D : {wr}")
    log("─── Scan terminé ───")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def cmd_status():
    etat = charger_etat()
    trades = etat["trades"]
    wr     = etat["victoires"] / trades * 100 if trades > 0 else 0.0

    print(f"\n{'═'*48}")
    print(f"  POLOXO — Statut")
    print(f"{'═'*48}")
    mode = "SIMULATION" if MODE_SIMU else "RÉEL"
    print(f"  Mode       : {mode}")
    print(f"  Balance    : {etat['balance']:.2f}$")
    print(f"  Trades     : {trades}")
    print(f"  Victoires  : {etat['victoires']}")
    print(f"  Défaites   : {etat['defaites']}")
    print(f"  Win rate   : {wr:.1f}%")

    print(f"\n  Positions ouvertes :")
    ouverts = 0
    for fichier in sorted(MARKETS_DIR.glob("paris_*.json")):
        with open(fichier) as f:
            mkt = json.load(f)
        if mkt["statut"] == "positionné" and mkt.get("position"):
            pos = mkt["position"]
            print(
                f"  • {mkt['date']} | {pos['bucket']}"
                f" | {pos['montant']}$ @ {pos['prix']:.3f}"
                f" | EV={pos['ev']:.1%}"
            )
            ouverts += 1
    if ouverts == 0:
        print("  Aucune")

    cal = charger_calibration().get("paris_ecmwf", {})
    if cal.get("n", 0) > 0:
        print(f"\n  Calibration ECMWF Paris : σ={cal['sigma']}°C (n={cal['n']})")
    print()


def cmd_report():
    print(f"\n{'═'*56}")
    print(f"  POLOXO — Rapport complet")
    print(f"{'═'*56}")

    lignes = []
    for fichier in sorted(MARKETS_DIR.glob("paris_*.json")):
        with open(fichier) as f:
            mkt = json.load(f)
        if mkt.get("position"):
            lignes.append(mkt)

    if not lignes:
        print("  Aucun trade enregistré.\n")
        return

    total_pnl = 0.0
    for mkt in lignes:
        pos  = mkt["position"]
        pnl  = mkt.get("pnl")
        if pnl is not None:
            signe    = "+" if pnl > 0 else ""
            icone    = "✓" if pnl > 0 else "✗"
            pnl_str  = f"{signe}{pnl:.2f}$"
            total_pnl += pnl
        else:
            icone   = "·"
            pnl_str = "en cours"

        print(
            f"  {icone} {mkt['date']}"
            f" | {pos['bucket']}"
            f" | mise={pos['montant']}$"
            f" | {pnl_str}"
        )

    signe_total = "+" if total_pnl >= 0 else ""
    print(f"\n  PnL total : {signe_total}{total_pnl:.2f}$")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1:
        commande = sys.argv[1].lower()
        if commande == "status":
            cmd_status()
        elif commande == "report":
            cmd_report()
        else:
            print(f"Commande inconnue : {commande}")
            print("Usage : python weatherbet.py [status|report]")
        return

    mode_label = "SIMULATION" if MODE_SIMU else "RÉEL"
    log(f"{'═'*40}")
    log(f"  POLOXO v2 démarré [{mode_label}]")
    log(f"  Balance : {BALANCE}$ | Mise max : {MAX_BET}$")
    log(f"  EV min  : {MIN_EV*100:.0f}% | Scan : {SCAN_INTERVAL//60} min")
    log(f"{'═'*40}")

    discord(
        f"🚀 **Poloxo démarré [{mode_label}]**\n"
        f"Balance : {BALANCE}$ | Mise max : {MAX_BET}$ | EV min : {MIN_EV*100:.0f}%"
    )

    while True:
        try:
            scan()
        except KeyboardInterrupt:
            log("Arrêt manuel")
            discord("🛑 **Poloxo arrêté**")
            break
        except Exception as e:
            log(f"Erreur critique : {e}", "ERR")
            traceback.print_exc()

        try:
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            log("Arrêt manuel")
            discord("🛑 **Poloxo arrêté**")
            break


if __name__ == "__main__":
    main()
