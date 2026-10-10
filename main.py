"""Alap futball-elemző: az aktuális forduló meccseire ad tippeket Tippmix piacokra.

Piacok: 1X2, Gólszám 2,5, Mindkét csapat szerez gólt, Szögletszám, Sárga lapok.
Az élő és aznapi eredményeket (fetch_live) és a The Odds API-ból lekért
szorzókat (fetch_odds; kulcs: ODDS_API_KEY) a webes felület használja.
A becslés a csapatok meccsenkénti átlagaiból (gól, szöglet, sárga lap) és az
utolsó meccsek formájából számolt várható értékekre épülő Poisson-modell,
amelyet a két csapat legutóbbi egymás elleni meccsei (H2H), a hiányzó
játékosok és a játékvezető lapátlaga módosít. A gólokhoz a lövésekből becsült
xG / xGA nagyobb súllyal számít, mint a tényleges gólok; a forma külön hazai
és vendég szerepre készül. További tényezők: kulcsemberek hiánya, tabellahelyzet
(motiváció), helyi rangadó, pihenőnapok, nemzetközi kupameccsek, új edző és
időjárás. A várható
gólszámhoz illő gólvonalakat (1,5 / 2,5 / 3,5) is megvizsgálja, és a 70%
feletti esélyű kimeneteleket ajánlott tippként emeli ki.

Használat:
    python main.py                      # Premier League
    python main.py --league SP1         # La Liga
    python main.py --short              # csak az összesítő táblázat
    python main.py --corner-line 10.5   # más szöglethatár (ugyanígy: --card-line)
    python main.py --table              # a tippek elé a tabellát is kiírja
    python main.py --weather rain       # esős forduló (extreme: szélsőséges idő)
    python main.py --team-news hirek.json   # kezdőcsapatok, hiányzók, bírók fájlból
                                            # (minta: team_news.example.json)
"""

import argparse
import io
import json
import math
import os
import sys
import textwrap
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ElementTree
from datetime import date
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# --- The Odds API (the-odds-api.com): automatikus szorzók -------------------------
# A kulcs a projektmappa .env fájljában van (ODDS_API_KEY=...); a környezeti változó és az
# alábbi sor is megfelel, a webes felületen pedig az oldalsáv kulcsmezője felülírhatja.
ENV_FILE = Path(__file__).with_name(".env")
ODDS_API_KEY = ""
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
ODDS_API_SPORTS = {
    "E0": "soccer_epl",
    "SP1": "soccer_spain_la_liga",
    "D1": "soccer_germany_bundesliga",
    "I1": "soccer_italy_serie_a",
    "F1": "soccer_france_ligue_one",
}
# Felületen választható iroda -> a The Odds API irodakulcsai, elsőbbségi sorrendben. A Tippmixpro
# nincs az API-ban; ugyanarra a Kambi-rendszerre épül, mint az Unibet, ezért annak a szorzóit kapja.
# Az API-ban csak országonkénti Unibet van: a holland és a svéd szorzói egyeznek (a francia eltér).
UNIBET_KEYS = ("unibet_nl", "unibet_se")
ODDS_API_BOOKMAKERS = {"Tippmixpro": UNIBET_KEYS, "Unibet": UNIBET_KEYS}
ODDS_API_MARKETS = "h2h,totals"  # 1X2 és gólszám; egy lekérés piaconként egy egységet fogyaszt a keretből

# Ingyenes, kulcs nélküli adatforrás (football-data.co.uk).
DATA_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"

LEAGUES = {
    "E0": "Premier League",
    "SP1": "La Liga",
    "D1": "Bundesliga",
    "I1": "Serie A",
    "F1": "Ligue 1",
}

# Statisztika -> (hazai, vendég) oszlop a forrásfájlban.
STATS = {
    "goals": ("FTHG", "FTAG"),
    "corners": ("HC", "AC"),
    "yellows": ("HY", "AY"),
}

# Lövésadatok a forrásfájlban; ezekből becsüljük az xG-t (valódi xG nincs a forrásban).
SHOT_COLUMNS = {
    "shots": ("HS", "AS"),
    "shots_on_target": ("HST", "AST"),
}
XG_WEIGHT = 0.65  # a várható gólban ennyit számít az xG-alapú becslés a tényleges gólok mellett
XG_MIN_ROWS = 50  # ennél kevesebb lövésadatos csapatmeccsből nem becsül xG-t
XG_FALLBACK = (0.30, 0.03)  # gól / kaput eltaláló, illetve mellé menő lövés, ha az illesztés értelmetlen

VENUE_FORM_SHARE = 0.6  # a formán belül ennyit számít a pályán (hazai, illetve vendégként) mutatott forma
FORM_MATCHES = 5  # ennyi utolsó meccs számít formának
FORM_WEIGHT = 0.4  # a forma súlya a hosszabb távú átlaggal szemben
PREV_SEASON_WEIGHT = 0.5  # az előző szezon meccsei ennyit érnek az ideiekhez képest
PRIOR_MATCHES = 5  # ennyi "átlagos" meccsel húzzuk a ligaátlag felé a kevés adatot
H2H_WEIGHT = 0.30  # az egymás elleni meccsek súlya a várható értékekben, ha megvan mind a H2H_MATCHES
H2H_MATCHES = 5  # ennyi utolsó egymás elleni meccs számít
H2H_SEASONS = 5  # ennyi szezonra visszamenőleg keresi az egymás elleni meccseket
MAX_GOALS = 10  # eddig a gólszámig összegezzük a Poisson-eloszlást
CONFIDENT = 0.60  # e fölötti valószínűségű tipp kap csillagot

GOAL_LINE = 2.5
CORNER_LINE = 9.5
CARD_LINE = 4.5
GOAL_LINES = (1.5, 2.5, 3.5)  # a dinamikus gólhatár ezek közül választ
RECOMMENDED = 0.70  # e fölötti valószínűségű kimenetel ajánlott tipp

# Motiváció: a szezon második felében a bajnoki címért vagy a kiesés ellen küzdő csapat.
MOTIVATION_FROM = 0.5  # a szezon ekkora hányada után számít a tabellahelyzet
MOTIVATION_GAP = 6  # ennyi ponton belül van "harcban" a csapat az éllel, illetve a kieső zónával
MOTIVATION_ATTACK = 0.06  # a rúgott gól szorzójának növekedése a szezon végén (addig arányosan kisebb)
MOTIVATION_DEFENCE = 0.03  # a kapott gól szorzójának csökkenése
RELEGATION_SPOTS = 3

DERBY_CARD_FACTOR = 1.15  # helyi rangadón ennyivel több a sárga lap
DERBY_FORM_SHARE = 0.5  # rangadón a friss forma súlya ennyi a szokásoshoz képest
NEW_COACH_BOOST = (0.05, -0.03)  # új edző első meccsein a (rúgott gól, kapott gól) szorzó változása

# Nemzetközi kupák az ESPN-nél: a bajnoki előtti kupameccs a pihenőnapokat rövidíti,
# a bajnoki utáni CUP_WINDOW napon belüli kupameccs előtt rotációval számol.
CUP_COMPETITIONS = {
    "uefa.champions": "Bajnokok Ligája",
    "uefa.europa": "Európa-liga",
    "uefa.europa.conf": "Konferencia-liga",
}
CUP_WINDOW = 4
CUP_ROTATION = (-0.05, 0.03)  # közelgő kupameccs hatása a (rúgott gól, kapott gól) szorzóra
CUP_NAME_MATCH = 0.9  # pontos vagy egész szavas névegyezés kell, hogy egy kupameccset a csapathoz kössünk

RISK_ABSENCES = 3  # ennél több hiányzó alapember egy csapatnál: magas kockázatú meccs
REST_SHORT = 3  # legfeljebb ennyi nap pihenő számít kevésnek; eggyel több nap fele hatású
REST_IMPACT = (-0.04, 0.08)  # kevés pihenő hatása a (rúgott gól, kapott gól) szorzóra

# Időjárás -> (megnevezés, gólszorzó, sárgalap-szorzó). Becsült értékek.
WEATHER = {
    "normal": ("normál", 1.00, 1.00),
    "rain": ("esős", 0.94, 1.06),
    "extreme": ("szélsőséges", 0.86, 1.12),
}

DROP_THRESHOLD = 0.05  # a szorzó ekkora arányú esése számít piaci dropnak
ODDS_CORRECTION_SECONDS = 120  # ennyi időn belüli átírás javításnak számít, nem piaci mozgásnak

# Helyi rangadók és nagy rivalizálások, a football-data.co.uk csapatneveivel.
DERBIES = {
    frozenset(pair)
    for pair in [
        ("Arsenal", "Tottenham"), ("Arsenal", "Chelsea"), ("Chelsea", "Tottenham"), ("Chelsea", "Fulham"),
        ("Chelsea", "Brentford"), ("Fulham", "Brentford"), ("West Ham", "Tottenham"), ("West Ham", "Chelsea"),
        ("West Ham", "Arsenal"), ("Crystal Palace", "Brighton"), ("Liverpool", "Everton"),
        ("Man United", "Man City"), ("Man United", "Liverpool"), ("Man United", "Leeds"),
        ("Newcastle", "Sunderland"), ("Aston Villa", "Wolves"), ("Aston Villa", "Birmingham"),
        ("Aston Villa", "West Brom"), ("Wolves", "West Brom"), ("Nott'm Forest", "Derby"),
        ("Nott'm Forest", "Leicester"), ("Coventry", "Leicester"), ("Coventry", "Aston Villa"),
        ("Southampton", "Portsmouth"), ("Ipswich", "Norwich"), ("Sheffield United", "Sheffield Weds"),
        ("Real Madrid", "Ath Madrid"), ("Real Madrid", "Barcelona"), ("Real Madrid", "Getafe"),
        ("Real Madrid", "Vallecano"), ("Ath Madrid", "Getafe"), ("Ath Madrid", "Vallecano"),
        ("Getafe", "Leganes"), ("Barcelona", "Espanol"), ("Barcelona", "Girona"), ("Sevilla", "Betis"),
        ("Ath Bilbao", "Sociedad"), ("Ath Bilbao", "Alaves"), ("Ath Bilbao", "Osasuna"),
        ("Valencia", "Levante"), ("Valencia", "Villarreal"), ("Celta", "La Coruna"), ("Oviedo", "Sp Gijon"),
        ("Dortmund", "Schalke 04"), ("Dortmund", "Bayern Munich"), ("Hamburg", "Werder Bremen"),
        ("Hamburg", "St Pauli"), ("FC Koln", "M'gladbach"), ("FC Koln", "Leverkusen"),
        ("Ein Frankfurt", "Mainz"), ("Stuttgart", "Freiburg"), ("Union Berlin", "Hertha"),
        ("Bayern Munich", "Nurnberg"), ("Bayern Munich", "Augsburg"),
        ("Inter", "Milan"), ("Roma", "Lazio"), ("Juventus", "Torino"), ("Juventus", "Inter"),
        ("Genoa", "Sampdoria"), ("Napoli", "Roma"), ("Fiorentina", "Juventus"), ("Atalanta", "Brescia"),
        ("Verona", "Chievo"), ("Bologna", "Fiorentina"), ("Pisa", "Fiorentina"), ("Parma", "Bologna"),
        ("Paris SG", "Marseille"), ("Paris SG", "Paris FC"), ("Lyon", "St Etienne"), ("Lyon", "Marseille"),
        ("Nice", "Monaco"), ("Lens", "Lille"), ("Nantes", "Rennes"), ("Rennes", "Brest"),
        ("Brest", "Lorient"), ("Le Havre", "Caen"), ("Metz", "Nancy"), ("Bordeaux", "Toulouse"),
    ]
}

# Élő és aznapi eredmények: kulcs nélküli ESPN eredményjelző, bajnokságonként.
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
ESPN_LEAGUES = {"E0": "eng.1", "SP1": "esp.1", "D1": "ger.1", "I1": "ita.1", "F1": "fra.1"}
MATCH_MINUTES = 90
LIVE_MAX_DAYS = 6  # egy forduló legfeljebb ennyi napjára kérdez le élő adatot
LIVE_NAME_MATCH = 0.6  # ekkora névegyezés kell, hogy egy élő meccset a kiírt meccshez kössünk
LIVE_KICKOFF_HOURS = 36  # legfeljebb ennyivel térhet el a két forrás kezdési ideje
# football-data.co.uk név -> az élő forrásban használt név, ahol a kettő nagyon eltér.
LIVE_ALIASES = {
    "Ath Bilbao": "Athletic Club",
    "Ath Madrid": "Atletico Madrid",
    "Paris SG": "Paris Saint-Germain",
    "FC Koln": "FC Cologne",
    "M'gladbach": "Borussia Monchengladbach",
    "La Coruna": "Deportivo",
    "Nott'm Forest": "Nottingham Forest",
    "Wolves": "Wolverhampton Wanderers",
    "Sp Gijon": "Sporting Gijon",
    "Milan": "AC Milan",
}

# Ligánkénti hírforrások: (név, RSS-cím). Cím nélkül a forrásnak nincs géppel olvasható hírfolyama,
# azt csak kézzel lehet követni (a felület ilyenkor a nevét sorolja fel).
NEWS_SOURCES = {
    "E0": [
        ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"),
        ("Sky Sports", "https://www.skysports.com/rss/12040"),
        ("Premier Injuries", None),
    ],
    "SP1": [
        ("Marca", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
        ("AS", "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/subsection/primera/"),
        ("Relevo", None),
    ],
    "I1": [
        ("La Gazzetta dello Sport", "https://www.gazzetta.it/rss/serie-a.xml"),
        ("Football Italia", "https://football-italia.net/feed/"),
    ],
    "D1": [
        ("Kicker", "https://newsfeed.kicker.de/news/bundesliga"),
        ("Bulinews", "https://bulinews.com/rss.xml"),
    ],
    "F1": [
        ("L'Équipe", "https://dwh.lequipe.fr/api/edito/rss?path=/Football/Ligue-1/"),
        ("RMC Sport", "https://rmcsport.bfmtv.com/rss/football/ligue-1/"),
    ],
}
NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (foci-tippek hírolvasó)"}
# Kategória -> szókezdetek, illetve kifejezések (ékezet nélkül, kisbetűvel) a források nyelvein.
NEWS_KEYWORDS = {
    "sérülés": [
        "injur", "ruled out", "sidelined", "hamstring", "setback", "lesion", "baja", "infortun",
        "verletz", "ausfall", "fallt aus", "bless", "forfait",
    ],
    "eltiltás": [
        "suspend", "suspensi", "banned", "red card", "sancion", "squalific", "gesperr", "sperre", "carton rouge",
    ],
    "kezdőcsapat": [
        "lineup", "line up", "team news", "starting xi", "alineacion", "formazion", "aufstellung",
        "startelf", "compo",
    ],
    "edzőváltás": [
        "sacked", "sacks", "new manager", "new head coach", "appoint", "destitu", "cesado", "nuevo entrenador",
        "esoner", "nuovo allenatore", "entlass", "freigestellt", "neuer trainer", "trainerwechsel", "limoge",
        "nouvel entraineur",
    ],
    "rotáció": ["rotat", "rested", "rotacion", "turnover", "geschont", "schont", "fait tourner", "au repos"],
    "sajtótájékoztató": [
        "press conference", "rueda de prensa", "conferenza stampa", "pressekonferenz", "conference de presse",
    ],
}
NEWS_MAX_AGE_DAYS = 7  # ennél régebbi cikk nem számít friss hírnek (néhány hírfolyam évekre visszamenő)
SQUAD_NEWS = ("sérülés", "eltiltás", "kezdőcsapat")  # a hiányzókat és kezdőket érintő kategóriák

USUAL_STARTS = 3  # aki a csapat utolsó meccsei közül legalább ennyin kezdett, az szokásos kezdő
USUAL_MIN_GAMES = 3  # ennél kevesebb ismert kezdőcsapatból nem állapít meg szokásos kezdőt

BOOKMAKERS = ["Tippmixpro", "Bet365", "Unibet", "Bwin", "William Hill", "Pinnacle"]  # az első az alapértelmezett

# Egy hiányzó kulcsjátékos hatása pozíció szerint: (rúgott gól, kapott gól, saját
# sárga lap) szorzójának változása. Becsült értékek, nem mért adatból származnak.
ABSENCE_IMPACT = {
    "F": (-0.08, 0.00, 0.00),
    "M": (-0.05, 0.03, 0.00),
    "D": (-0.01, 0.06, 0.05),
    "G": (0.00, 0.08, 0.00),
}
# Kulcsember (top góllövő, első számú kapus, kulcs-belsővédő, irányító) hiánya: ugyanilyen szorzóváltozás,
# de 15–20%-os nagyságrendben.
STAR_IMPACT = {
    "F": (-0.18, 0.00, 0.00),
    "M": (-0.15, 0.05, 0.00),
    "D": (-0.02, 0.16, 0.05),
    "G": (0.00, 0.18, 0.00),
}
NON_KEY_SHARE = 0.25  # nem kulcsjátékos hiánya ennyit számít a kulcsjátékoséhoz képest
DOUBTFUL_SHARE = 0.5  # kérdéses játékos ennyit számít a biztos hiányzóhoz képest
MAX_IMPACT = 0.35  # egy csapat szorzója legfeljebb ennyivel térhet el 1-től
REFEREE_PRIOR = 10  # ennyi "átlagos" meccsel húzzuk a ligaátlag felé a bíró lapátlagát
SIGNIFICANT = 0.05  # ekkora esélyváltozástól jelöljük meg a tippet (!)

REASONS = {"injury": "sérült", "suspension": "eltiltott"}
WEEKDAYS = ["H", "K", "Sze", "Cs", "P", "Szo", "V"]
NAN = float("nan")


def season_code(start_year: int) -> str:
    """2026 -> '2627'."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season_start() -> int:
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


def download_csv(url: str) -> pd.DataFrame:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.content.decode("utf-8-sig", errors="replace")))


def fetch_matches(league: str, season: str) -> pd.DataFrame:
    """Letölti a szezon meccseit, és a lejátszottakat adja vissza."""
    raw = download_csv(DATA_URL.format(season=season, league=league))
    matches = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["Date"], dayfirst=True),
            "home": raw["HomeTeam"],
            "away": raw["AwayTeam"],
        }
    )
    # Játékvezető csak néhány bajnokság fájljában van.
    matches["referee"] = raw["Referee"].str.strip() if "Referee" in raw else NAN
    for stat, (home_column, away_column) in {**STATS, **SHOT_COLUMNS}.items():
        matches[f"home_{stat}"] = raw.get(home_column, NAN)
        matches[f"away_{stat}"] = raw.get(away_column, NAN)
    matches = matches.dropna(subset=["home_goals", "away_goals"])
    return matches.astype({"home_goals": int, "away_goals": int})


def fetch_fixtures(league: str) -> pd.DataFrame:
    """Letölti a bajnokság következő fordulójának meccseit, budapesti idő szerint."""
    fixtures = download_csv(FIXTURES_URL)
    fixtures = fixtures[fixtures["Div"] == league].rename(
        columns={"HomeTeam": "home", "AwayTeam": "away"}
    )
    # A forrás brit idő szerint adja meg a kezdést.
    kickoff = pd.to_datetime(fixtures["Date"] + " " + fixtures["Time"], dayfirst=True)
    fixtures["kickoff"] = kickoff.dt.tz_localize("Europe/London").dt.tz_convert("Europe/Budapest")
    fixtures["referee"] = fixtures["Referee"].str.strip() if "Referee" in fixtures else NAN
    columns = ["kickoff", "home", "away", "referee"]
    return fixtures[columns].sort_values("kickoff").reset_index(drop=True)


def parse_absences(side: dict, key_names: set, confirmed: bool) -> list:
    """Egy csapat hiányzói a felállás-adatból.

    Hiányzó az, aki a missingPlayers listában van, illetve – kihirdetett
    kezdőcsapatnál – az a kulcsjátékos, aki nincs a kezdők között.
    """
    absences = []
    for entry in side.get("missingPlayers", []):
        player = entry.get("player", {})
        name = player.get("name", "?")
        doubtful = entry.get("type") == "doubtful"
        star = bool(entry.get("star"))
        key = star or bool(entry.get("key")) or name in key_names
        reason = REASONS.get(entry.get("reason"), str(entry.get("reason") or "hiányzik"))
        absences.append(
            {
                "name": name,
                "position": str(player.get("position") or "M")[:1].upper(),
                "reason": reason + (", kérdéses" if doubtful else ""),
                "key": key,
                "star": star,
                "weight": (1.0 if key else NON_KEY_SHARE) * (DOUBTFUL_SHARE if doubtful else 1.0),
            }
        )
    if confirmed:
        entries = side.get("players", [])
        starters = {
            entry.get("player", {}).get("name") for entry in entries if not entry.get("substitute")
        }
        positions = {
            entry.get("player", {}).get("name"): entry.get("player", {}).get("position")
            for entry in entries
        }
        listed = {absence["name"] for absence in absences}
        for name in sorted(key_names - starters - listed):
            absences.append(
                {
                    "name": name,
                    "position": str(positions.get(name) or "M")[:1].upper(),
                    "reason": "nincs a kezdőben",
                    "key": True,
                    "weight": 1.0,
                }
            )
    return absences


def fetch_news(league: str) -> tuple:
    """A liga hírforrásainak friss cikkei: (cikkek a legújabbtól, a nem elérhető források nevei).

    Egy cikk: {"source", "title", "link", "published" (időbélyeg vagy None), "summary"}.
    """
    items, failed = [], []
    newest = pd.Timestamp.now(tz="Europe/Budapest") - pd.Timedelta(days=NEWS_MAX_AGE_DAYS)
    for source, url in NEWS_SOURCES.get(league, []):
        if not url:
            continue
        try:
            response = requests.get(url, headers=NEWS_HEADERS, timeout=20)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except (requests.RequestException, ElementTree.ParseError):
            failed.append(source)
            continue
        for item in root.iter("item"):
            published = None
            try:
                published = pd.Timestamp(parsedate_to_datetime(item.findtext("pubDate", ""))).tz_convert("Europe/Budapest")
            except (TypeError, ValueError):
                pass
            if published is not None and published < newest:
                continue
            items.append(
                {
                    "source": source,
                    "title": " ".join((item.findtext("title") or "").split()),
                    "link": (item.findtext("link") or "").strip(),
                    "published": published,
                    "summary": item.findtext("description") or "",
                }
            )
    items.sort(key=lambda item: item["published"] or pd.Timestamp(0, tz="UTC"), reverse=True)
    return items, failed


def tag_news(items: list, teams: list) -> list:
    """A meccsek szempontjából fontos cikkek, amelyek a megadott csapatokat említik.

    A cikkek "teams" és "categories" mezőt kapnak; ami nem említ csapatot vagy
    nem illik egyik kategóriába sem (NEWS_KEYWORDS: sérülés, eltiltás,
    kezdőcsapat, edzőváltás, rotáció, sajtótájékoztató), kimarad.
    """
    names = {team: {plain_name(team), plain_name(LIVE_ALIASES.get(team, team))} for team in teams}
    tagged = []
    for item in items:
        text = f" {plain_name(item['title'] + ' ' + item['summary'])} "
        mentioned = [team for team, variants in names.items() if any(f" {name} " in text for name in variants)]
        categories = [
            category
            for category, keywords in NEWS_KEYWORDS.items()
            if any(f" {keyword}" in text for keyword in keywords)
        ]
        if mentioned and categories:
            tagged.append({**item, "teams": mentioned, "categories": categories})
    return tagged


def line_position(abbreviation: str) -> str:
    """Az élő forrás posztrövidítéséből (CD-L, RB, AM, F...) a modell posztja: G, D, M vagy F."""
    code = (abbreviation or "").upper()
    if code.startswith("G"):
        return "G"
    if code.startswith(("CD", "D", "SW")) or code.endswith("B") or "B-" in code:
        return "D"
    return "M" if "M" in code else "F"


def fetch_lineup_espn(league: str, event_id: str):
    """Egy meccs kihirdetett kezdőcsapatai az ESPN-ről, vagy None, ha még nincs kihirdetve.

    Oldalanként ("home" / "away"): csapatazonosító, felállás, kezdők (név, poszt),
    kispad, és a csapat előző meccseinek azonosítói (a szokásos kezdők megállapításához).
    """
    response = requests.get(
        ESPN_URL.format(league=ESPN_LEAGUES[league]).replace("scoreboard", "summary"),
        params={"event": event_id},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    recent = {
        entry["team"]["id"]: [event["id"] for event in entry.get("events", [])]
        for entry in data.get("lastFiveGames", [])
    }
    lineup = {}
    for roster in data.get("rosters", []):
        players = roster.get("roster", [])
        starters = [
            {
                "name": player["athlete"]["displayName"],
                "position": line_position(player.get("position", {}).get("abbreviation")),
            }
            for player in players
            if player.get("starter")
        ]
        if not starters:
            return None
        lineup[roster["homeAway"]] = {
            "team_id": roster["team"]["id"],
            "formation": roster.get("formation") or "",
            "starters": starters,
            "bench": [player["athlete"]["displayName"] for player in players if not player.get("starter")],
            "recent": recent.get(roster["team"]["id"], []),
        }
    return lineup if set(lineup) == {"home", "away"} else None


def usual_starters(league: str, side: dict, fetch=fetch_lineup_espn) -> dict:
    """A csapat szokásos kezdői az előző meccsei alapján: név -> poszt.

    Szokásos kezdő, aki az ismert kezdőcsapatok közül legalább USUAL_STARTS-ban
    szerepelt. A fetch(league, meccsazonosító) a hívó (gyorsítótárazott) letöltője lehet.
    """
    starts, positions, games = {}, {}, 0
    for event_id in side["recent"]:
        try:
            lineup = fetch(league, event_id)
        except (requests.RequestException, ValueError, KeyError):
            continue
        own = next((team for team in (lineup or {}).values() if team["team_id"] == side["team_id"]), None)
        if own is None:
            continue
        games += 1
        for player in own["starters"]:
            starts[player["name"]] = starts.get(player["name"], 0) + 1
            positions[player["name"]] = player["position"]
    if games < USUAL_MIN_GAMES:
        return {}
    return {name: positions[name] for name, count in starts.items() if count >= USUAL_STARTS}


def lineup_absences(side: dict, usual: dict) -> list:
    """A kihirdetett kezdőből hiányzó szokásos kezdők, a hiányzók alakjában (lásd parse_absences).

    Aki a keretben sincs, teljes súllyal számít; aki a kispadon ül, DOUBTFUL_SHARE súllyal.
    """
    starting = {player["name"] for player in side["starters"]}
    absences = []
    for name, position in sorted(usual.items()):
        if name in starting:
            continue
        benched = name in side["bench"]
        absences.append(
            {
                "name": name,
                "position": position,
                "reason": "kispadon kezd" if benched else "nincs a keretben",
                "key": True,
                "star": position == "G",  # a szokásos kezdőkapus az első számú kapus
                "weight": DOUBTFUL_SHARE if benched else 1.0,
            }
        )
    return absences


def load_team_news(source: str) -> dict:
    """Kezdőcsapatok, hiányzók és játékvezetők beolvasása JSON fájlból vagy URL-ről.

    A meccsenkénti "lineups" rész a Sofascore felállás-adatainak szerkezetét
    követi (home/away -> players[], missingPlayers[]); a teljes formátumra
    a team_news.example.json a minta. A csapatneveknek egyezniük kell a
    football-data.co.uk neveivel. Eredmény: (hazai, vendég) -> hírek.
    """
    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=30)
        response.raise_for_status()
        data = response.json()
    else:
        with open(source, encoding="utf-8") as file:
            data = json.load(file)
    return parse_team_news(data)


def parse_team_news(data: dict) -> dict:
    """A beolvasott hírfájl tartalmából a meccsenkénti hírek (lásd load_team_news)."""
    key_players = data.get("keyPlayers", {})
    news = {}
    for match in data.get("matches", []):
        home, away = match["home"], match["away"]
        lineups = match.get("lineups", {})
        confirmed = bool(lineups.get("confirmed"))
        referee = match.get("referee")
        news[(home, away)] = {
            "referee": {"name": referee} if isinstance(referee, str) else referee,
            "home": parse_absences(lineups.get("home", {}), set(key_players.get(home, [])), confirmed),
            "away": parse_absences(lineups.get("away", {}), set(key_players.get(away, [])), confirmed),
        }
    return news


def team_rows(matches: pd.DataFrame) -> pd.DataFrame:
    """Minden meccsből egy hazai és egy vendég sort csinál (<stat>_for / <stat>_against, venue: home / away)."""

    def side(own: str, other: str) -> pd.DataFrame:
        columns = {own: "team"}
        for stat in (*STATS, "xg"):
            columns[f"{own}_{stat}"] = f"{stat}_for"
            columns[f"{other}_{stat}"] = f"{stat}_against"
        return matches.drop(columns=other).rename(columns=columns).assign(venue=own)

    return pd.concat([side("home", "away"), side("away", "home")], ignore_index=True)


def build_table(matches: pd.DataFrame) -> pd.DataFrame:
    """Tabellát számol a lejátszott meccsekből."""
    rows = team_rows(matches)[["team", "goals_for", "goals_against"]]

    rows["played"] = 1
    rows["won"] = (rows["goals_for"] > rows["goals_against"]).astype(int)
    rows["drawn"] = (rows["goals_for"] == rows["goals_against"]).astype(int)
    rows["lost"] = (rows["goals_for"] < rows["goals_against"]).astype(int)

    table = rows.groupby("team").sum()
    table["goal_diff"] = table["goals_for"] - table["goals_against"]
    table["points"] = 3 * table["won"] + table["drawn"]

    table = table.sort_values(
        ["points", "goal_diff", "goals_for"], ascending=False
    ).reset_index()
    table.index += 1
    return table[
        ["team", "played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_diff", "points"]
    ]


def team_form(history: pd.DataFrame) -> pd.Series:
    """Csapatonként az utolsó FORM_MATCHES meccs eredménye, a régebbitől az újabbig."""
    rows = team_rows(history).sort_values("date", kind="stable")
    recent = rows.groupby("team").tail(FORM_MATCHES).copy()
    recent["result"] = "D"
    recent.loc[recent["goals_for"] > recent["goals_against"], "result"] = "GY"
    recent.loc[recent["goals_for"] < recent["goals_against"], "result"] = "V"
    return recent.groupby("team")["result"].agg(" ".join)


def stat_model(history: pd.DataFrame, stat: str, form_weight: float = FORM_WEIGHT):
    """Egy statisztika (gól, xG, szöglet, sárga lap) modellje, vagy None, ha nincs hozzá adat.

    Csapatonként a saját és az ellenfélnél mért érték erőssége a ligaátlaghoz
    képest (1.0 = átlagos), külön hazai és vendég szerepre: a hosszabb távú
    átlagot (súlyozva, a ligaátlag felé húzva) keveri a formával. A forma az
    utolsó FORM_MATCHES meccs átlaga, amelyben az adott pályán játszott meccsek
    VENUE_FORM_SHARE súllyal számítanak.
    """
    own, opponent = f"{stat}_for", f"{stat}_against"
    rows = team_rows(history).dropna(subset=[own, opponent]).sort_values("date", kind="stable")
    played = history.dropna(subset=[f"home_{stat}", f"away_{stat}"])
    if rows.empty:
        return None
    league_avg = (rows[own] * rows["weight"]).sum() / rows["weight"].sum()

    weighted = (
        rows.assign(own=rows[own] * rows["weight"], opponent=rows[opponent] * rows["weight"])
        .groupby("team")[["own", "opponent", "weight"]]
        .sum()
    )
    matches_with_prior = weighted["weight"] + PRIOR_MATCHES
    own_avg = (weighted["own"] + PRIOR_MATCHES * league_avg) / matches_with_prior
    opponent_avg = (weighted["opponent"] + PRIOR_MATCHES * league_avg) / matches_with_prior

    def recent(subset: pd.DataFrame) -> pd.DataFrame:
        return subset.groupby("team").tail(FORM_MATCHES).groupby("team")[[own, opponent]].mean()

    form = recent(rows)
    teams = pd.DataFrame({"own_avg": own_avg, "opponent_avg": opponent_avg})
    for venue in ("home", "away"):
        # Akinek az adott pályán még nincs meccse, annál az általános forma marad.
        at_venue = recent(rows[rows["venue"] == venue]).reindex(form.index)
        mixed = (VENUE_FORM_SHARE * at_venue + (1 - VENUE_FORM_SHARE) * form).fillna(form)
        teams[f"own_strength_{venue}"] = ((1 - form_weight) * own_avg + form_weight * mixed[own]) / league_avg
        teams[f"opponent_strength_{venue}"] = (
            (1 - form_weight) * opponent_avg + form_weight * mixed[opponent]
        ) / league_avg
    weights = played["weight"]
    return {
        "teams": teams,
        "home_avg": (played[f"home_{stat}"] * weights).sum() / weights.sum(),
        "away_avg": (played[f"away_{stat}"] * weights).sum() / weights.sum(),
    }


def expected(model, home: str, away: str) -> tuple:
    """A hazai és a vendég csapat várható értéke a modell statisztikájából."""
    if model is None:
        return NAN, NAN
    teams = model["teams"]
    return (
        model["home_avg"]
        * teams["own_strength_home"].get(home, 1.0)
        * teams["opponent_strength_away"].get(away, 1.0),
        model["away_avg"]
        * teams["own_strength_away"].get(away, 1.0)
        * teams["opponent_strength_home"].get(home, 1.0),
    )


def plain_name(name: str) -> str:
    """Csapatnév összehasonlításhoz: ékezetek és írásjelek nélkül, kisbetűvel."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def name_score(team: str, candidates: list) -> float:
    """Mennyire egyezik a football-data csapatnév az élő forrás valamelyik névváltozatával (0–1)."""
    best = 0.0
    for target in {plain_name(team), plain_name(LIVE_ALIASES.get(team, team))}:
        for candidate in candidates:
            other = plain_name(candidate)
            if not other:
                continue
            if target == other:
                return 1.0
            words = other.split()
            if f" {target} " in f" {other} " or f" {other} " in f" {target} ":
                score = 0.9
            elif all(any(word.startswith(part) for word in words) for part in target.split()):
                score = 0.85  # rövidítés: "man united" – "manchester united"
            else:
                score = SequenceMatcher(None, target, other).ratio()
            best = max(best, score)
    return best


def fetch_live_espn(league: str, first_day, last_day) -> list:
    """Az ESPN eredményjelzőjéről a bajnokság meccsei a két nap között, egységes alakban.

    A forrás egyszerre egy napot ad vissza, ezért naponként kérdez (legfeljebb LIVE_MAX_DAYS napot).
    """
    raw = {}
    for day in pd.date_range(first_day, last_day)[:LIVE_MAX_DAYS]:
        response = requests.get(
            ESPN_URL.format(league=ESPN_LEAGUES[league]), params={"dates": f"{day:%Y%m%d}"}, timeout=20
        )
        response.raise_for_status()
        for event in response.json().get("events", []):
            raw[event["id"]] = event
    events = []
    for event in raw.values():
        competition = event["competitions"][0]
        sides = {side["homeAway"]: side for side in competition["competitors"]}
        if set(sides) != {"home", "away"}:
            continue
        status = event["status"]
        kind = status["type"]
        state = "finished" if kind.get("completed") else "live" if kind.get("state") == "in" else "upcoming"

        def stat(side: dict, name: str):
            for entry in side.get("statistics", []):
                if entry.get("name") == name:
                    try:
                        return float(entry.get("displayValue"))
                    except (TypeError, ValueError):
                        return None
            return None

        def yellows(side: dict):
            return sum(
                1
                for detail in competition.get("details", [])
                if detail.get("yellowCard") and detail.get("team", {}).get("id") == side["team"]["id"]
            )

        def names(side: dict) -> list:
            team = side["team"]
            return [team.get(key, "") for key in ("displayName", "shortDisplayName", "name", "location")]

        started = state != "upcoming"
        corners = (stat(sides["home"], "wonCorners"), stat(sides["away"], "wonCorners"))
        events.append(
            {
                "home_names": names(sides["home"]),
                "away_names": names(sides["away"]),
                "id": event["id"],
                "kickoff": pd.Timestamp(event["date"]),
                "state": state,
                "clock": status.get("displayClock", ""),
                "minute": float(status.get("clock") or 0) / 60,
                # Félbeszakadt, elhalasztott vagy törölt meccs: nem élő és nincs végeredménye.
                "note": kind.get("description", "") if kind.get("state") == "post" and state != "finished" else "",
                "half_time": kind.get("name") == "STATUS_HALFTIME",
                "goals": (int(sides["home"].get("score") or 0), int(sides["away"].get("score") or 0)) if started else None,
                "corners": corners if started and None not in corners else None,
                "yellows": (yellows(sides["home"]), yellows(sides["away"])) if started else None,
            }
        )
    return events


# Élő adatforrások: név -> letöltő(league, első nap, utolsó nap) -> események listája.
# Új forrás (például kulcsos API) ugyanilyen alakú eseményeket adó függvénnyel vehető fel.
LIVE_SOURCES = {"espn": fetch_live_espn}


def fetch_live(league: str, first_day, last_day, source: str = "espn") -> list:
    """A bajnokság élő, befejezett és kiírt meccsei a két nap között a választott forrásból."""
    return LIVE_SOURCES[source](league, first_day, last_day)


def match_live(fixtures: pd.DataFrame, events: list) -> dict:
    """A kiírt meccsekhez a hozzájuk tartozó élő esemény: (hazai, vendég) -> esemény.

    A csapatnevek forrásonként eltérnek, ezért a kezdési időhöz közeli események
    közül a legjobb névegyezésűt választja; bizonytalan egyezésnél nem párosít.
    """
    matched = {}
    for fixture in fixtures.itertuples():
        kickoff = pd.Timestamp(fixture.kickoff)
        kickoff = kickoff.tz_localize("Europe/Budapest") if kickoff.tzinfo is None else kickoff
        best, best_score = None, LIVE_NAME_MATCH
        for event in events:
            if abs((event["kickoff"] - kickoff).total_seconds()) > LIVE_KICKOFF_HOURS * 3600:
                continue
            score = min(name_score(fixture.home, event["home_names"]), name_score(fixture.away, event["away_names"]))
            if score > best_score:
                best, best_score = event, score
        if best is not None:
            matched[(fixture.home, fixture.away)] = best
    return matched


class OddsApiError(Exception):
    """A The Odds API hibája (hiányzó vagy rossz kulcs, elfogyott keret) a szolgáltató üzenetével."""


def read_env_file(path: Path = ENV_FILE) -> dict:
    """A .env fájl KULCS=érték sorai; hiányzó fájlnál üres. A # kezdetű és az üres sorok kimaradnak."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        name, separator, value = line.strip().partition("=")
        if separator and name and not name.startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    return values


def odds_api_key() -> str:
    """A The Odds API kulcsa: a kódban megadott, különben a környezeti változó, különben a .env fájl."""
    return ODDS_API_KEY or os.environ.get("ODDS_API_KEY", "") or read_env_file().get("ODDS_API_KEY", "")


def parse_odds(payload: list, bookmaker_keys: tuple) -> list:
    """A The Odds API válaszából meccsenként az iroda szorzói, a modell piacneveivel.

    Meccsenként a bookmaker_keys közül az első olyan irodát veszi, amelyiktől érkezett adat.

    Egy esemény: {"home_names", "away_names", "kickoff", "updated", "prices"}, ahol a
    prices kulcsa (piac, kimenetel) – például ("1X2", "X") vagy ("Gólszám 2,5", "Több").
    Csak a ténylegesen megérkezett, értelmes szorzók kerülnek bele; ami hiányzik, az hiányzik.
    """
    events = []
    for event in payload:
        home, away = event.get("home_team", ""), event.get("away_team", "")
        prices, updated = {}, None
        offered = {bookmaker.get("key"): bookmaker for bookmaker in event.get("bookmakers", [])}
        for bookmaker in [offered[key] for key in bookmaker_keys if key in offered][:1]:
            updated = bookmaker.get("last_update")
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    if isinstance(price, bool) or not isinstance(price, (int, float)) or not 1.01 <= price < 1000:
                        continue
                    if market.get("key") == "h2h":
                        side = {home: "1", away: "2", "Draw": "X"}.get(outcome.get("name"))
                        if side:
                            prices[("1X2", side)] = float(price)
                    elif market.get("key") == "totals" and outcome.get("point") in GOAL_LINES:
                        side = {"Over": "Több", "Under": "Kevesebb"}.get(outcome.get("name"))
                        if side:
                            prices[(f"Gólszám {line_text(outcome['point'])}", side)] = float(price)
        events.append(
            {
                "home_names": [home],
                "away_names": [away],
                "kickoff": pd.Timestamp(event["commence_time"]),
                "updated": pd.Timestamp(updated) if updated else None,
                "prices": prices,
            }
        )
    return events


def fetch_odds(league: str, bookmaker_keys: tuple, api_key: str) -> tuple:
    """Az iroda szorzói a bajnokság kiírt meccseire: (események, a havi keretből hátralévő kérések vagy None)."""
    response = requests.get(
        ODDS_API_URL.format(sport=ODDS_API_SPORTS[league]),
        params={
            "apiKey": api_key,
            "bookmakers": ",".join(bookmaker_keys),
            "markets": ODDS_API_MARKETS,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        timeout=20,
    )
    if response.status_code != 200:
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = ""
        raise OddsApiError(message or f"HTTP {response.status_code}")
    return parse_odds(response.json(), bookmaker_keys), response.headers.get("x-requests-remaining")


def match_odds(fixtures: pd.DataFrame, events: list) -> dict:
    """A kiírt meccsekhez a kezdés előtti szorzók: (hazai, vendég) -> {(piac, kimenetel): szorzó}.

    A már elkezdődött meccsek élő szorzói kimaradnak, mert a modell meccs előtti esélyt ad.
    """
    now = pd.Timestamp.now(tz="UTC")
    return {
        pair: event["prices"]
        for pair, event in match_live(fixtures, events).items()
        if event["prices"] and event["kickoff"] > now
    }


def remaining_minutes(event: dict):
    """Egy élő meccsből hátralévő rendes játékidő percben (a hosszabbítás nélkül)."""
    if event["state"] != "live":
        return None
    return int(max(0, MATCH_MINUTES - (45 if event.get("half_time") else event["minute"])))


def outcome_won(row: dict, score: dict):
    """Nyert-e a kimenetel (lásd selections) az adott állással; None, ha a szükséges adat hiányzik."""
    values = score.get(row["stat"])
    if not values or any(pd.isna(value) for value in values):
        return None
    home, away = values
    if row["kind"] == "1x2":
        return row["outcome"] == ("1" if home > away else "2" if away > home else "X")
    if row["kind"] == "btts":
        return (row["outcome"] == "Igen") == (home > 0 and away > 0)
    return (row["outcome"] == "Több") == (home + away > row["line"])


def add_xg(matches: pd.DataFrame) -> pd.DataFrame:
    """A meccsekhez csapatonként becsült xG-t számol a lövésekből (home_xg / away_xg oszlop).

    A forrásban nincs mért xG, ezért a kaput eltaláló és a mellé menő lövések
    gólértékét a meccsekre illeszti, majd úgy skálázza, hogy az összes xG
    megegyezzen az összes góllal. Lövésadat nélkül az oszlopok üresek maradnak.
    """
    matches = matches.copy()
    if not {f"{side}_{column}" for side in ("home", "away") for column in SHOT_COLUMNS} <= set(matches.columns):
        return matches.assign(home_xg=NAN, away_xg=NAN)
    sides = pd.concat(
        [
            pd.DataFrame(
                {
                    "on": matches[f"{side}_shots_on_target"],
                    "off": matches[f"{side}_shots"] - matches[f"{side}_shots_on_target"],
                    "goals": matches[f"{side}_goals"],
                }
            )
            for side in ("home", "away")
        ]
    ).dropna()
    if len(sides) < XG_MIN_ROWS:
        return matches.assign(home_xg=NAN, away_xg=NAN)

    shots = sides[["on", "off"]].to_numpy(dtype=float)
    on_value, off_value = np.linalg.lstsq(shots, sides["goals"].to_numpy(dtype=float), rcond=None)[0]
    if on_value <= 0 or off_value < 0:
        on_value, off_value = XG_FALLBACK
    scale = sides["goals"].sum() / (shots @ np.array([on_value, off_value])).sum()
    for side in ("home", "away"):
        on_target = matches[f"{side}_shots_on_target"]
        matches[f"{side}_xg"] = scale * (on_value * on_target + off_value * (matches[f"{side}_shots"] - on_target))
    return matches


def build_models(history: pd.DataFrame) -> dict:
    """A gól-, szöglet- és lapmodell, valamint az xG-modell ("xg"; None, ha nincs lövésadat).

    A "derby" kulcs alatt ugyanezek rangadóra: ott a friss forma csak DERBY_FORM_SHARE súllyal számít.
    """
    history = add_xg(history)
    stats = (*STATS, "xg")
    models = {stat: stat_model(history, stat) for stat in stats}
    models["derby"] = {stat: stat_model(history, stat, FORM_WEIGHT * DERBY_FORM_SHARE) for stat in stats}
    return models


def is_derby(home: str, away: str) -> bool:
    return frozenset((home, away)) in DERBIES


def rest_days(history: pd.DataFrame, team: str, kickoff):
    """Hány nap telt el a csapat előző bajnokija óta, vagy None, ha nincs korábbi meccse.

    Csak a letöltött bajnoki meccseket látja – kupa- és nemzetközi meccset nem.
    """
    day = pd.Timestamp(kickoff).tz_localize(None).normalize()
    dates = history.loc[(history["home"] == team) | (history["away"] == team), "date"]
    dates = dates[dates < day]
    return None if dates.empty else int((day - dates.max().normalize()).days)


def fatigue_factors(days) -> tuple:
    """A pihenőnapokból a csapat (rúgott gól, kapott gól) szorzója."""
    if days is None or pd.isna(days) or days > REST_SHORT + 1:
        return 1.0, 1.0
    share = 1.0 if days <= REST_SHORT else 0.5
    return 1 + REST_IMPACT[0] * share, 1 + REST_IMPACT[1] * share


def motivation(table, team: str) -> tuple:
    """A csapat tétje a tabella alapján: (erősség 0–1, leírás), tét nélkül (0.0, None).

    Csak a szezon MOTIVATION_FROM hányada után számít; az erősség a szezon
    előrehaladtával nő. Harcban az van, aki MOTIVATION_GAP ponton belül áll az
    éllovastól, illetve a kieső zóna legjobbjától.
    """
    if table is None or table.empty or team not in set(table["team"]):
        return 0.0, None
    rounds = 2 * (len(table) - 1)
    position = int(table.index[table["team"] == team][0])
    row = table.loc[position]
    progress = float(row["played"] / rounds)
    if progress < MOTIVATION_FROM:
        return 0.0, None
    gap = min(MOTIVATION_GAP, 3 * (rounds - row["played"]))
    behind = int(table["points"].iloc[0] - row["points"])
    above_drop = int(row["points"] - table["points"].iloc[-RELEGATION_SPOTS])
    if behind <= gap:
        return progress, f"bajnoki címért küzd ({position}. hely, {behind} pont hátrány)"
    if above_drop <= gap:
        zone = "kieső helyen" if position > len(table) - RELEGATION_SPOTS else f"{above_drop} pontra a kieső zónától"
        return progress, f"kiesés ellen küzd ({position}. hely, {zone})"
    return 0.0, None


def fetch_cup_matches(first_day, last_day) -> list:
    """A nemzetközi kupák (CUP_COMPETITIONS) meccsei a két nap között az ESPN-ről.

    Egy meccs: {"day" (budapesti naptári nap), "competition", "home_names", "away_names"}.
    Kupameccs kedd és csütörtök között van, ezért csak ezeket a napokat kérdezi; ami nem tölthető le, kimarad.
    """

    def one(request: tuple) -> list:
        league, day = request
        try:
            response = requests.get(ESPN_URL.format(league=league), params={"dates": f"{day:%Y%m%d}"}, timeout=20)
            response.raise_for_status()
            events = response.json().get("events", [])
        except (requests.RequestException, ValueError):
            return []
        matches = []
        for event in events:
            sides = {side["homeAway"]: side["team"] for side in event["competitions"][0]["competitors"]}
            if set(sides) != {"home", "away"}:
                continue
            matches.append(
                {
                    "day": pd.Timestamp(event["date"]).tz_convert("Europe/Budapest").tz_localize(None).normalize(),
                    "competition": CUP_COMPETITIONS[league],
                    "home_names": [sides["home"].get(key, "") for key in ("displayName", "shortDisplayName", "name")],
                    "away_names": [sides["away"].get(key, "") for key in ("displayName", "shortDisplayName", "name")],
                }
            )
        return matches

    requests_to_make = [
        (league, day)
        for day in pd.date_range(first_day, last_day)
        if day.weekday() in (1, 2, 3)
        for league in CUP_COMPETITIONS
    ]
    if not requests_to_make:
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [match for matches in pool.map(one, requests_to_make) for match in matches]


def cup_schedule(team: str, kickoff, cups) -> dict:
    """A csapat legközelebbi kupameccse a bajnoki előtt és után CUP_WINDOW napon belül.

    Eredmény: {"before": (nap, kupa) vagy None, "after": (nap, kupa) vagy None}.
    """
    day = pd.Timestamp(kickoff).tz_localize(None).normalize()
    found = {"before": None, "after": None}
    for match in cups or []:
        if max(name_score(team, match["home_names"]), name_score(team, match["away_names"])) < CUP_NAME_MATCH:
            continue
        gap = int((match["day"] - day).days)
        side = "before" if -CUP_WINDOW <= gap < 0 else "after" if 0 < gap <= CUP_WINDOW else None
        if side and (found[side] is None or abs(gap) < found[side][0]):
            found[side] = (abs(gap), match["competition"])
    return found


def match_context(match, history: pd.DataFrame, table=None, weather: str = "normal", cups=None) -> dict:
    """Egy meccs körülményei, mindenhol (hazai, vendég) sorrendben.

    Időjárás, rangadó, pihenőnapok (a bajnoki és a megadott kupameccsek alapján),
    közeli kupameccsek, új edző (alapból nincs; a hívó állíthatja) és motiváció.
    """
    cup = tuple(cup_schedule(team, match.kickoff, cups) for team in (match.home, match.away))
    rest = []
    for team, schedule in zip((match.home, match.away), cup):
        days = [rest_days(history, team, match.kickoff)] + ([schedule["before"][0]] if schedule["before"] else [])
        days = [value for value in days if value is not None]
        rest.append(min(days) if days else None)
    return {
        "weather": weather,
        "derby": is_derby(match.home, match.away),
        "rest": tuple(rest),
        "cup": cup,
        "new_coach": (False, False),
        "motivation": (motivation(table, match.home), motivation(table, match.away)),
    }


def record_odds(entries: list, odds, now: float) -> list:
    """A szorzó naplója ([időbélyeg, szorzó] párok) az új értékkel kiegészítve.

    Változatlan érték nem kerül be; az ODDS_CORRECTION_SECONDS-on belüli átírás
    az előző bejegyzést írja felül, hogy az elgépelés ne tűnjön piaci mozgásnak.
    """
    if entries and entries[-1][1] == odds:
        return entries
    if not entries:
        return entries if odds is None else [[now, odds]]
    if now - entries[-1][0] < ODDS_CORRECTION_SECONDS:
        return [*entries[:-1], [entries[-1][0], odds]]
    return [*entries, [now, odds]]


def odds_movement(entries: list):
    """A naplózott szorzó mozgása: nyitó és mostani érték, arányos változás, és hogy drop-e; különben None."""
    values = [odds for _, odds in entries if odds]
    if len(values) < 2 or not entries[-1][1] or values[0] == values[-1]:
        return None
    change = values[-1] / values[0] - 1
    return {"opening": values[0], "current": values[-1], "change": change, "drop": change <= -DROP_THRESHOLD}


def older_seasons(league: str, season_start: int, fetch=None) -> list:
    """Az előző szezonnál régebbi szezonok meccsei az egymás elleni mérleghez; ami nem tölthető le, kimarad.

    A fetch(league, kezdőév) függvénnyel a hívó saját (például gyorsítótárazott) letöltőjét adhatja meg.
    """
    if fetch is None:
        fetch = lambda league, start_year: fetch_matches(league, season_code(start_year))  # noqa: E731
    seasons = []
    for back in range(2, H2H_SEASONS):
        try:
            seasons.append(fetch(league, season_start - back))
        except (requests.RequestException, KeyError, ValueError):
            continue
    return seasons


def h2h_meetings(matches: pd.DataFrame, home: str, away: str) -> pd.DataFrame:
    """A két csapat egymás elleni meccsei (bármelyik pályán), a régebbitől az újabbig."""
    return matches[
        ((matches["home"] == home) & (matches["away"] == away))
        | ((matches["home"] == away) & (matches["away"] == home))
    ].sort_values("date", kind="stable")


def apply_h2h(values: dict, models: dict, matches: pd.DataFrame, home: str, away: str) -> dict:
    """A várható értékeket a két csapat utolsó H2H_MATCHES egymás elleni meccsének átlaga felé húzza.

    A súly a meglévő meccsek arányában nő H2H_WEIGHT-ig, így egy-két találkozó
    csak keveset számít. A fordított pályaválasztású meccsek értékeit a liga
    hazai/vendég átlagának arányával számolja át a mostani felállásra.
    """
    pair = h2h_meetings(matches, home, away)
    blended = dict(values)
    for stat in STATS:
        model = models[stat]
        meetings = pair.dropna(subset=[f"home_{stat}", f"away_{stat}"]).tail(H2H_MATCHES)
        if model is None or meetings.empty or not model["away_avg"] or not model["home_avg"]:
            continue
        same = (meetings["home"] == home).astype(float)  # 1, ha akkor is a mostani hazai csapat volt otthon
        venue = model["home_avg"] / model["away_avg"]
        hosts, guests = meetings[f"home_{stat}"], meetings[f"away_{stat}"]
        home_value = (hosts * same + guests * venue * (1 - same)).mean()
        away_value = (guests * same + hosts / venue * (1 - same)).mean()
        weight = H2H_WEIGHT * len(meetings) / H2H_MATCHES
        blended[stat] = (
            (1 - weight) * values[stat][0] + weight * home_value,
            (1 - weight) * values[stat][1] + weight * away_value,
        )
    return blended


def poisson(expected_value: float, k: int) -> float:
    return math.exp(-expected_value) * expected_value**k / math.factorial(k)


def predict(home_xg: float, away_xg: float) -> dict:
    """A két várható gólszámból kiszámolja a gólpiacok kimeneteleinek valószínűségét."""
    probs = {"1": 0.0, "X": 0.0, "2": 0.0, "over": 0.0, "btts": 0.0}
    for home_goals in range(MAX_GOALS + 1):
        for away_goals in range(MAX_GOALS + 1):
            p = poisson(home_xg, home_goals) * poisson(away_xg, away_goals)
            if home_goals > away_goals:
                probs["1"] += p
            elif home_goals == away_goals:
                probs["X"] += p
            else:
                probs["2"] += p
            if home_goals + away_goals > GOAL_LINE:
                probs["over"] += p
            if home_goals > 0 and away_goals > 0:
                probs["btts"] += p
    return probs


def over_under(expected_total: float, line: float) -> dict:
    """Több/Kevesebb esély egy Poisson-eloszlású összegre (szöglet, lap)."""
    under = sum(poisson(expected_total, k) for k in range(int(line) + 1))
    return {"Több": 1 - under, "Kevesebb": under}


def line_text(line: float) -> str:
    return f"{line:g}".replace(".", ",")


def goal_lines(expected_total: float) -> list:
    """A várható gólszámhoz illő gólvonalak: (határ, oldal) párok.

    A várható érték alatti határoknál a Több, a felettieknél a Kevesebb az
    esélyesebb oldal – 2,8 várható gólnál: 1,5 és 2,5 felett, 3,5 alatt.
    """
    return [(line, "Több" if line < expected_total else "Kevesebb") for line in GOAL_LINES]


def implied_probability(odds: float) -> float:
    """A szorzóba árazott valószínűség (az iroda árrésével együtt)."""
    return 1 / odds


def selections(analysis: dict) -> list:
    """Egy meccs megfogadható kimenetelei a modell esélyével: 1X2, gólvonalak, mindkét csapat gólja, szöglet, lap.

    A "group" az összetartozó piacokat jelöli (a három gólvonal egy csoport);
    a "kind", "stat" és "line" alapján dönthető el utólag, nyert-e (lásd outcome_won).
    """
    markets = analysis["markets"]
    expected_total = sum(markets[0]["values"])
    rows = [
        {"group": "1X2", "market": "1X2", "outcome": outcome, "p": p, "kind": "1x2", "stat": "goals", "line": None}
        for outcome, p in markets[0]["outcomes"].items()
    ]
    for line in GOAL_LINES:
        for outcome, p in over_under(expected_total, line).items():
            rows.append(
                {
                    "group": "Gólszám",
                    "market": f"Gólszám {line_text(line)}",
                    "outcome": outcome,
                    "p": p,
                    "kind": "total",
                    "stat": "goals",
                    "line": line,
                }
            )
    for market in markets[2:]:
        for outcome, p in (market["outcomes"] or {}).items():
            rows.append(
                {
                    "group": market["name"],
                    "market": market["name"],
                    "outcome": outcome,
                    "p": p,
                    "kind": "total" if "line" in market else "btts",
                    "stat": market.get("stat", "goals"),
                    "line": market.get("line"),
                }
            )
    return rows


def recommended(analysis: dict) -> list:
    """A RECOMMENDED fölötti esélyű kimenetelek, a legvalószínűbbel kezdve."""
    picks = [row for row in selections(analysis) if row["p"] > RECOMMENDED]
    return sorted(picks, key=lambda row: row["p"], reverse=True)


def absence_factors(absences: list) -> tuple:
    """A hiányzókból a csapat (rúgott gól, kapott gól, sárga lap) szorzója."""
    totals = [0.0, 0.0, 0.0]
    for absence in absences:
        impacts = STAR_IMPACT if absence.get("star") else ABSENCE_IMPACT
        impact = impacts.get(absence["position"], impacts["M"])
        for index, change in enumerate(impact):
            totals[index] += change * absence["weight"]
    return tuple(1 + max(-MAX_IMPACT, min(MAX_IMPACT, total)) for total in totals)


def referee_factor(history: pd.DataFrame, referee) -> tuple:
    """A játékvezető lapszorzója a ligaátlaghoz képest, és a hozzá tartozó leírás.

    A lapátlagot a letöltött meccsekből számolja; ha ott nem szerepel a bíró,
    a hírfájlban megadott yellowCards / games értékből.
    """
    played = history.dropna(subset=["home_yellows", "away_yellows"])
    if not referee or played.empty:
        return 1.0, None
    cards = (played["home_yellows"] + played["away_yellows"]) * played["weight"]
    league_avg = cards.sum() / played["weight"].sum()

    own = played["referee"] == referee["name"]
    own_cards, games = cards[own].sum(), played.loc[own, "weight"].sum()
    if games == 0 and referee.get("games"):
        own_cards, games = float(referee.get("yellowCards", 0)), float(referee["games"])
    if games == 0:
        return 1.0, f"{referee['name']} (nincs róla lapadat)"

    factor = (own_cards + REFEREE_PRIOR * league_avg) / (games + REFEREE_PRIOR) / league_avg
    text = (
        f"{referee['name']}, {own_cards / games:.2f} sárga/meccs "
        f"(ligaátlag {league_avg:.2f}) → lapok ×{factor:.2f}"
    )
    return factor, text


def build_markets(values: dict, corner_line: float, card_line: float) -> list:
    """A várható értékekből (stat -> (hazai, vendég)) a piacok és esélyeik."""
    probs = predict(*values["goals"])
    markets = [
        {
            "name": "1X2",
            "short": "1X2",
            "values": values["goals"],
            "outcomes": {"1": probs["1"], "X": probs["X"], "2": probs["2"]},
        },
        {
            "name": f"Gólszám {line_text(GOAL_LINE)}",
            "short": f"Gól {line_text(GOAL_LINE)}",
            "values": None,
            "outcomes": {"Több": probs["over"], "Kevesebb": 1 - probs["over"]},
        },
        {
            "name": "Mindkét csapat szerez gólt",
            "short": "Mindk.gól",
            "values": None,
            "outcomes": {"Igen": probs["btts"], "Nem": 1 - probs["btts"]},
        },
    ]
    for name, short, stat, line in (
        ("Szögletszám", "Szögl", "corners", corner_line),
        ("Sárga lapok", "Lap", "yellows", card_line),
    ):
        home_value, away_value = values[stat]
        markets.append(
            {
                "name": f"{name} {line_text(line)}",
                "short": f"{short} {line_text(line)}",
                "stat": stat,
                "line": line,
                "values": values[stat],
                "outcomes": None
                if math.isnan(home_value)
                else over_under(home_value + away_value, line),
            }
        )
    return markets


def best(outcomes: dict) -> tuple:
    return max(outcomes.items(), key=lambda item: item[1])


def short_tip(tip: str) -> str:
    return "Kev." if tip == "Kevesebb" else tip


def influence(before, after) -> str:
    """Megjegyzés, ha a módosítók megfordították a tippet vagy jelentősen változtattak az esélyén."""
    if before is None or after is None:
        return ""
    tip, p = best(after)
    old_tip, _ = best(before)
    if tip != old_tip:
        return f"volt: {short_tip(old_tip)}"
    change = p - before[tip]
    if abs(change) >= SIGNIFICANT:
        return f"{change * 100:+.0f} %p"
    return ""


def analyse(
    match,
    models: dict,
    history: pd.DataFrame,
    news: dict,
    corner_line: float,
    card_line: float,
    h2h=None,
    context=None,
) -> dict:
    """Egy meccs piacai minden tényezővel módosítva, plusz a megjegyzések.

    A várható gól a gól- és az xG-modell keveréke (az xG nagyobb súllyal), amit az
    egymás elleni meccsek (a h2h táblából, ennek híján a history-ból) finomítanak.
    Erre jönnek a szorzók: hiányzók, játékvezető, és a context-ből (lásd
    match_context) az időjárás, a rangadó, a pihenőnapok, a közeli kupameccs, az
    új edző és a motiváció. Rangadón a forma kisebb súlyú modelljei számolnak.
    """
    home, away = match.home, match.away
    context = context or {}
    derby = bool(context.get("derby"))
    if derby and "derby" in models:
        models = models["derby"]
    base = {stat: expected(models[stat], home, away) for stat in STATS}
    if models.get("xg") is not None:
        chances = expected(models["xg"], home, away)
        if not any(math.isnan(value) for value in chances):
            base["goals"] = tuple(
                (1 - XG_WEIGHT) * goals + XG_WEIGHT * chance for goals, chance in zip(base["goals"], chances)
            )
    base = apply_h2h(base, models, history if h2h is None else h2h, home, away)

    team_news = news.get((home, away), {})
    absences = {home: team_news.get("home", []), away: team_news.get("away", [])}
    missing = {team: absence_factors(absences[team]) for team in (home, away)}

    referee = team_news.get("referee")
    if referee is None and isinstance(match.referee, str):
        referee = {"name": match.referee}
    card_factor, referee_text = referee_factor(history, referee)

    weather, weather_goals, weather_cards = WEATHER[context.get("weather", "normal")]
    derby_cards = DERBY_CARD_FACTOR if derby else 1.0
    rest = dict(zip((home, away), context.get("rest", (None, None))))
    stakes = dict(zip((home, away), context.get("motivation", ((0.0, None), (0.0, None)))))
    cup = dict(zip((home, away), context.get("cup", ({}, {}))))
    new_coach = dict(zip((home, away), context.get("new_coach", (False, False))))
    tired = {team: fatigue_factors(rest[team]) for team in (home, away)}
    driven = {
        team: (1 + MOTIVATION_ATTACK * stakes[team][0], 1 - MOTIVATION_DEFENCE * stakes[team][0])
        for team in (home, away)
    }
    # Közelgő nemzetközi kupameccs előtt rotáció várható; új edzővel rövid távú lendület.
    rotated = {
        team: (1 + CUP_ROTATION[0], 1 + CUP_ROTATION[1]) if (cup[team] or {}).get("after") else (1.0, 1.0)
        for team in (home, away)
    }
    lifted = {
        team: (1 + NEW_COACH_BOOST[0], 1 + NEW_COACH_BOOST[1]) if new_coach[team] else (1.0, 1.0)
        for team in (home, away)
    }

    def goal_factor(team: str, opponent: str) -> float:
        attack = missing[team][0] * tired[team][0] * driven[team][0] * rotated[team][0] * lifted[team][0]
        concede = (
            missing[opponent][1] * tired[opponent][1] * driven[opponent][1] * rotated[opponent][1] * lifted[opponent][1]
        )
        return attack * concede * weather_goals

    adjusted = dict(base)
    adjusted["goals"] = (
        base["goals"][0] * goal_factor(home, away),
        base["goals"][1] * goal_factor(away, home),
    )
    adjusted["yellows"] = tuple(
        value * card_factor * missing[team][2] * derby_cards * weather_cards
        for value, team in zip(base["yellows"], (home, away))
    )

    markets = build_markets(adjusted, corner_line, card_line)
    for market, before in zip(markets, build_markets(base, corner_line, card_line)):
        market["note"] = influence(before["outcomes"], market["outcomes"])

    notes = []
    if referee_text:
        notes.append(f"Játékvezető: {referee_text}")
    if derby_cards != 1.0:
        notes.append(f"Rangadó: sárga lapok ×{derby_cards:.2f}, a friss forma súlya {DERBY_FORM_SHARE:.0%}-ra csökken")
    if (weather_goals, weather_cards) != (1.0, 1.0):
        notes.append(f"Időjárás: {weather} → gólok ×{weather_goals:.2f}, sárga lapok ×{weather_cards:.2f}")
    for team in (home, away):
        attack, concede, cards = missing[team]
        if absences[team]:
            players = ", ".join(
                f"{absence['name']} ({absence['position']}, {absence['reason']}"
                f"{', kulcsember' if absence.get('star') else ', kulcsjátékos' if absence['key'] else ''})"
                for absence in absences[team]
            )
            notes.append(
                f"Hiányzók – {team}: {players} → rúgott gól ×{attack:.2f}, "
                f"kapott gól ×{concede:.2f}, sárga lap ×{cards:.2f}"
            )
        if tired[team] != (1.0, 1.0):
            notes.append(
                f"Pihenő – {team}: {rest[team]:.0f} nap az előző meccs óta → "
                f"rúgott gól ×{tired[team][0]:.2f}, kapott gól ×{tired[team][1]:.2f}"
            )
        if stakes[team][1]:
            notes.append(
                f"Motiváció – {team}: {stakes[team][1]} → "
                f"rúgott gól ×{driven[team][0]:.2f}, kapott gól ×{driven[team][1]:.2f}"
            )
        schedule = cup[team] or {}
        if schedule.get("before"):
            notes.append(
                f"Nemzetközi kupa – {team}: {schedule['before'][1]}-meccs {schedule['before'][0]} nappal a bajnoki "
                f"előtt (a pihenőnapokba beszámítva)"
            )
        if schedule.get("after"):
            notes.append(
                f"Nemzetközi kupa – {team}: {schedule['after'][1]}-meccs {schedule['after'][0]} nap múlva, rotáció "
                f"várható → rúgott gól ×{rotated[team][0]:.2f}, kapott gól ×{rotated[team][1]:.2f}"
            )
        if new_coach[team]:
            notes.append(
                f"Új edző – {team}: rúgott gól ×{lifted[team][0]:.2f}, kapott gól ×{lifted[team][1]:.2f}"
            )
    # Hiányzó alapemberek száma csapatonként: a tippajánló ebből ítéli meg a meccs kockázatát.
    key_absences = {team: sum(1 for absence in absences[team] if absence["key"]) for team in (home, away)}
    return {"markets": markets, "notes": notes, "key_absences": key_absences}


def print_wrapped(text: str, indent: str) -> None:
    print(textwrap.fill(text, width=78, initial_indent=indent, subsequent_indent=indent + "  "))


def print_match(match, analysis: dict, models: dict, form: pd.Series) -> None:
    """Egy meccs részletes blokkja: csapatstatisztikák, várható értékek, majd piaconként egy sor."""
    markets = analysis["markets"]
    kickoff = match.kickoff
    print(f"{match.home} – {match.away}   ({WEEKDAYS[kickoff.weekday()]} {kickoff:%m.%d. %H:%M})")

    print(f"  {'Csapat':<18}{'Forma':<16}{'Gól':<14}{'Szöglet':<14}Sárga lap")
    for team in (match.home, match.away):
        cells = ""
        for stat in STATS:
            model = models[stat]
            if model is None or team not in model["teams"].index:
                cells += f"{'–':<14}"
                continue
            row = model["teams"].loc[team]
            cells += f"{row['own_avg']:.2f} / {row['opponent_avg']:.2f}".ljust(14)
        print(f"  {team:<18}{form.get(team, '–'):<16}{cells.rstrip()}")

    # A várható értékek a piacok közül azokból jönnek, amelyekhez tartozik csapatbontás.
    split = total = ""
    for market in markets:
        values = market["values"]
        if values is None:
            continue
        known = not math.isnan(values[0])
        split += (f"{values[0]:.2f} – {values[1]:.2f}" if known else "–").ljust(14)
        total += (f"{sum(values):.2f}" if known else "–").ljust(14)
    print(f"  {'Várható (hazai – vendég)':<34}{split.rstrip()}")
    print(f"  {'Várható összesen':<34}{total.rstrip()}")
    for note in analysis["notes"]:
        print_wrapped(note, "  ")

    print(f"  {'Piac':<27}{'Esélyek':<24}{'Tipp':<10}{'Fair':<8}Megj.")
    for market in markets:
        outcomes = market["outcomes"]
        if outcomes is None:
            print(f"  {market['name']:<27}nincs adat")
            continue
        tip, p = best(outcomes)
        chances = "  ".join(f"{label} {chance:.0%}" for label, chance in outcomes.items())
        fair = f"{1 / p:.2f}" + (" ★" if p >= CONFIDENT else "")
        note = f"! {market['note']}" if market["note"] else ""
        print(f"  {market['name']:<27}{chances:<24}{tip:<10}{fair:<8}{note}".rstrip())
    picks = recommended(analysis)
    if picks:
        text = "; ".join(
            f"{row['market']} {row['outcome']} {row['p']:.0%} (fair {1 / row['p']:.2f})" for row in picks
        )
        print_wrapped(f"Ajánlott tipp ({RECOMMENDED:.0%} felett): {text}", "  ")
    print()


def print_summary(rows: list) -> None:
    """Összesítő: meccsenként egy sor, piaconként a tipp és az esélye."""
    widths = [7, 10, 10, 11, 9]
    first_markets = rows[0][1]["markets"]
    header = "".join(f"{market['short']:<{width}}" for market, width in zip(first_markets, widths))
    print(f"{'Összesítő':<31}{header}".rstrip())
    print("-" * (31 + sum(widths)))
    flagged = []
    for match, analysis in rows:
        name = f"{match.home} – {match.away}"
        cells = ""
        changes = []
        for market, width in zip(analysis["markets"], widths):
            outcomes = market["outcomes"]
            if outcomes is None:
                cells += f"{'–':<{width}}"
                continue
            tip, p = best(outcomes)
            mark = "★" if p >= CONFIDENT else ""
            if market["note"]:
                mark = "!"
                changes.append(f"{market['short']} ({market['note']})")
            cells += f"{short_tip(tip)} {p:.0%}{mark}".ljust(width)
        print(f"{name[:30]:<31}{cells}".rstrip())
        if changes:
            flagged.append(f"! {name}: {', '.join(changes)}")
    print()
    if flagged:
        print(f"! = a módosítók (hiányzó, bíró, rangadó, pihenő, időjárás, motiváció) miatt megfordult, vagy legalább {SIGNIFICANT * 100:.0f} %p-ot változott:")
        for line in flagged:
            print_wrapped(line, "  ")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Alap futball-elemző – Tippmix tippek")
    parser.add_argument("--league", default="E0", choices=sorted(LEAGUES), help="bajnokság kódja")
    parser.add_argument("--table", action="store_true", help="a tabellát is kiírja")
    parser.add_argument("--short", action="store_true", help="csak az összesítő táblázat")
    parser.add_argument("--corner-line", type=float, default=CORNER_LINE, help="szöglethatár")
    parser.add_argument("--card-line", type=float, default=CARD_LINE, help="sárgalap-határ")
    parser.add_argument("--weather", default="normal", choices=sorted(WEATHER), help="időjárás a fordulóban")
    parser.add_argument("--team-news", metavar="FÁJL", help="kezdőcsapatok és hiányzók JSON fájlja vagy URL-je")
    args = parser.parse_args()

    news = {}
    if args.team_news:
        try:
            news = load_team_news(args.team_news)
        except (OSError, ValueError, KeyError, requests.RequestException) as error:
            print(f"Nem sikerült beolvasni a csapathíreket: {error!r}", file=sys.stderr)
            return 1

    season_start = current_season_start()
    try:
        current = fetch_matches(args.league, season_code(season_start))
        previous = fetch_matches(args.league, season_code(season_start - 1))
        fixtures = fetch_fixtures(args.league)
    except requests.RequestException as error:
        print(f"Nem sikerült letölteni az adatokat: {error}", file=sys.stderr)
        return 1

    league = LEAGUES[args.league]
    if args.table and not current.empty:
        print(f"{league} – tabella {len(current)} meccs után\n")
        print(build_table(current).to_string())
        print()

    if fixtures.empty:
        print(f"{league}: most nincs kiírt meccs a következő fordulóra.")
        return 0

    history = pd.concat(
        [previous.assign(weight=PREV_SEASON_WEIGHT), current.assign(weight=1.0)],
        ignore_index=True,
    )
    models = build_models(history)
    form = team_form(history)
    table = build_table(current) if not current.empty else None
    h2h = pd.concat([*older_seasons(args.league, season_start), previous, current], ignore_index=True)

    known = set(zip(fixtures["home"], fixtures["away"]))
    for home, away in news.keys() - known:
        print(f"Figyelem: a csapathírekben szereplő {home} – {away} nincs a fordulóban.", file=sys.stderr)

    print(f"{league} – tippek a következő forduló {len(fixtures)} meccsére")
    print(f"Forma: utolsó {FORM_MATCHES} meccs, a régebbitől az újabbig. ★ = legalább {CONFIDENT:.0%} esély.")
    print("Gól / szöglet / sárga lap: meccsenkénti átlag, saját / ellenfél.")
    print(f"A várható értékekben az utolsó {H2H_MATCHES} egymás elleni meccs legfeljebb {H2H_WEIGHT:.0%} súllyal számít.\n")
    rows = []
    for match in fixtures.itertuples():
        context = match_context(match, history, table, args.weather)
        analysis = analyse(match, models, history, news, args.corner_line, args.card_line, h2h, context)
        rows.append((match, analysis))
        if not args.short:
            print_match(match, analysis, models, form)
    print_summary(rows)
    print("Statisztikai becslés, nem garancia.")
    if not news:
        print("Hiányzókat és kezdőcsapatot csak --team-news fájlból lát.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
