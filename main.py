"""Alap futball-elemző: az aktuális forduló meccseire ad tippeket Tippmix piacokra.

Piacok: 1X2, Gólszám 2,5, Mindkét csapat szerez gólt, Szögletszám, Sárga lapok.
Az élő és aznapi eredményeket (fetch_live) és a The Odds API-ból lekért
szorzókat (fetch_odds; kulcs: ODDS_API_KEY) a webes felület használja.
A becslés a csapatok meccsenkénti átlagaiból (gól, szöglet, sárga lap) és az
utolsó meccsek formájából számolt várható értékekre épülő Poisson-modell,
amelyet a két csapat legutóbbi egymás elleni meccsei (H2H), a hiányzó
játékosok és a játékvezető lapátlaga módosít. A gólokhoz a csapatok erejét a
korábbi meccseik szorzóiból visszafejtett várható gólokból, a tényleges
gólokból és a lövésekből becsült xG-ből számolja (rating_model), a friss
meccseket nagyobb súllyal. További tényezők: kulcsemberek hiánya, tabellahelyzet
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
from functools import lru_cache
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
# alábbi sor is megfelel.
ENV_FILE = Path(__file__).with_name(".env")
ODDS_API_KEY = ""
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
ODDS_API_SPORTS = {
    "E0": "soccer_epl",
    "SP1": "soccer_spain_la_liga",
    "D1": "soccer_germany_bundesliga",
    "I1": "soccer_italy_serie_a",
    "F1": "soccer_france_ligue_one",
    "CL": "soccer_uefa_champs_league",
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
    "CL": "Bajnokok Ligája",
}
# Nemzetközi kupák: a football-data.co.uk-n nincsenek meg, ezért a meccsadataik az ESPN-ről jönnek; a gól és
# a szöglet becslése a csapatok hazai bajnoki erejére épül (lásd CUP_MODEL), a többi a kupameccsekre.
EUROPEAN_CUPS = {"CL"}
EUROPEAN_FROM_MONTH = 9  # a főtábla szeptemberben indul; a nyári selejtezők kimaradnak
# A kupacsapatok hazai bajnokságai a football-data.co.uk-n (teljes statisztikával), és a csoportjuk a ligaerősséghez.
CUP_DOMESTIC_LEAGUES = ["E0", "SP1", "D1", "I1", "F1", "N1", "P1", "B1", "T1", "G1", "SC0"]
CUP_LEAGUE_GROUP = {"B1": "MINOR", "T1": "MINOR", "G1": "MINOR", "SC0": "MINOR"}  # a többi liga saját csoport
# Kupameccs várható értéke a csapatok hazai bajnoki erejéből (1 = a saját ligája átlagos csapata):
#   base × (hazai pályán: home) × támadóerő^attack_power × a liga támadószorzója
#        × az ellenfél kapott-erő^defence_power × az ellenfél ligájának kapott-szorzója.
# A "leagues" a ligák erőssége: (támadás, kapott) szorzó – egy angol átlagcsapat a BL-ben 35%-kal több gólt
# szerez és 28%-kal kevesebbet kap, mint egy átlagos mezőnybeli. NONE: akinek a bajnokságáról nincs adat.
# Illesztve a 2023/24–2025/26-os BL 503 meccsére; külön szezonokon ellenőrizve (1X2 veszteség a csak
# BL-meccsekre épülő becsléshez képest: 0,936 → 0,890 és 0,944 → 0,927).
CUP_MODEL = {
    "goals": {"base": 1.166, "home": 1.331, "attack_power": 1.33, "defence_power": 0.89, "leagues": {"E0": (1.35, 0.72), "SP1": (1.23, 0.93), "D1": (1.13, 0.89), "I1": (0.89, 0.79), "F1": (1.16, 0.84), "N1": (0.88, 1.46), "P1": (0.79, 1.07), "MINOR": (0.73, 1.35), "NONE": (1.01, 1.21)}},
    "corners": {"base": 4.149, "home": 1.311, "attack_power": 0.72, "defence_power": 0.63, "leagues": {"E0": (1.10, 0.84), "SP1": (1.14, 0.94), "D1": (1.12, 0.95), "I1": (1.05, 0.92), "F1": (1.14, 0.79), "N1": (1.02, 1.19), "P1": (0.91, 1.17), "MINOR": (0.78, 1.07), "NONE": (0.82, 1.25)}},
}
CUP_PRIOR_MATCHES = 12  # a csapat kupameccsei ennyi „hazai bajnoki alapú" meccs súlyával szemben módosítják a becslést
CUP_UPDATE_DAYS = 500  # az ennél régebbi kupameccsek már nem módosítják a hazai bajnoki alapú becslést
CUP_NAME_ALIASES = {"Sp Lisbon": "Sporting CP"}  # football-data.co.uk név -> ESPN név, ahol nagyon eltér

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

# A fogadóirodák átlagszorzói a forrásfájlban, elsőbbségi sorrendben (a régebbi szezonok más oszlopnevet használnak).
ODDS_1X2 = [("AvgH", "AvgD", "AvgA"), ("BbAvH", "BbAvD", "BbAvA"), ("B365H", "B365D", "B365A")]
ODDS_TOTAL = [("Avg>2.5", "Avg<2.5"), ("BbAv>2.5", "BbAv<2.5"), ("B365>2.5", "B365<2.5")]
# Csapaterő a gólokhoz (rating_model): a csapatok korábbi meccseinek szorzóiból visszafejtett várható gólok a
# legjobb erőmérők, mert a piac a hiányzókat és a keret erejét is beárazta. Csak a már lejátszott meccsek
# szorzóit használja, a megtippelt meccsét nem. A visszamérésből illesztett értékek.
RATING_MARKET = 0.8  # a célértékben ennyit számít a szorzókból visszafejtett várható gól (a többi: gól és xG)
RATING_HALF_LIFE = 150  # ennyi nap alatt feleződik egy régebbi meccs súlya
RATING_PRIOR = 1.0  # ennyi "átlagos" meccsel húzza az átlag felé a csapaterőt
RATING_NEW_TEAM = (0.8, 1.2)  # az újonc (az előző szezonban nem szereplő) csapat kiinduló (támadó, kapott) ereje
RATING_STRETCH = 1.15  # a két csapat várható góljának arányát ennyivel a hatványra emeli (az esélyest alábecsülné)
RATING_MIN_SHARE = 0.5  # a meccsek legalább ekkora hányadához kell szorzó, különben a régi (gól + xG) becslés marad
IMPLIED_TOTAL = 2.7  # a visszafejtésnél ennyi az össz-gól, ha a meccshez nincs gólszám-szorzó

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
CORNER_LINE = 7.5
CARD_LINE = 3.5
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

# Játékstílus: a csapat labdabirtoklása és lövésszáma a liga többi csapatához képest.
STYLE_MIN_MATCHES = 5  # ennél kevesebb (súlyozott) meccsből nem állapít meg stílust
STYLE_NAMES = {"high": "labdatartó", "mid": "kiegyensúlyozott", "low": "visszaálló"}
ATTACK_NAMES = {"high": "sokat lövő", "mid": "", "low": "keveset lövő"}
# Mit hozott a múltban egy-egy párosítás: (hazai stílus, vendég stílus) -> meccsszám, gól, 2,5 gól felett,
# mindkét csapat gólja, szöglet (hazai, vendég), sárga lap, hazai győzelem, döntetlen. Mért adat: az öt
# bajnokság 2020/21–2026/27-es meccsei, a csapatok meccs előtti (utolsó 30 meccses) labdabirtoklása szerint.
STYLE_MATCHUPS = {
    ("high", "high"): {"n": 1112, "goals": 3.07, "over": 0.58, "btts": 0.60, "corners": (5.4, 4.1), "yellows": 4.1, "home": 0.44, "draw": 0.23},
    ("high", "mid"): {"n": 1231, "goals": 2.99, "over": 0.57, "btts": 0.54, "corners": (6.0, 3.7), "yellows": 3.9, "home": 0.59, "draw": 0.24},
    ("high", "low"): {"n": 1150, "goals": 3.02, "over": 0.57, "btts": 0.52, "corners": (6.7, 3.3), "yellows": 3.6, "home": 0.67, "draw": 0.20},
    ("mid", "high"): {"n": 1177, "goals": 2.88, "over": 0.56, "btts": 0.58, "corners": (4.5, 5.0), "yellows": 4.1, "home": 0.28, "draw": 0.25},
    ("mid", "mid"): {"n": 1096, "goals": 2.83, "over": 0.55, "btts": 0.57, "corners": (5.3, 4.4), "yellows": 4.2, "home": 0.43, "draw": 0.24},
    ("mid", "low"): {"n": 1216, "goals": 2.66, "over": 0.50, "btts": 0.53, "corners": (5.7, 4.0), "yellows": 4.1, "home": 0.50, "draw": 0.28},
    ("low", "high"): {"n": 1203, "goals": 2.82, "over": 0.53, "btts": 0.52, "corners": (4.2, 5.4), "yellows": 3.9, "home": 0.20, "draw": 0.25},
    ("low", "mid"): {"n": 1163, "goals": 2.63, "over": 0.49, "btts": 0.54, "corners": (4.8, 4.8), "yellows": 4.2, "home": 0.34, "draw": 0.30},
    ("low", "low"): {"n": 1091, "goals": 2.47, "over": 0.43, "btts": 0.53, "corners": (5.2, 4.3), "yellows": 4.2, "home": 0.41, "draw": 0.30},
}
STYLE_AVERAGE = {"goals": 2.82, "over": 0.53, "corners": 9.7, "yellows": 4.0, "draw": 0.25}
# Amit az átlagokra épülő modell a párosításból nem lát: (gólszorzó, sárgalap-szorzó, döntetlen-többlet).
# A visszamérésből illesztett értékek; ahol a modell magától is jól számol, ott 1 és 0.
STYLE_EFFECT = {
    ("high", "high"): (1.04, 1.00, 0.00),
    ("high", "mid"): (1.00, 0.98, 0.00),
    ("high", "low"): (1.03, 0.94, 0.00),
    ("mid", "mid"): (1.00, 0.97, 0.00),
    ("mid", "low"): (1.00, 0.98, 0.12),
    ("low", "high"): (0.97, 1.00, 0.10),
    ("low", "mid"): (0.97, 1.00, 0.20),
    ("low", "low"): (0.95, 1.00, 0.30),
}
# A csapatok közti különbségből ennyi marad a várható értékben (1 = a mért átlagok változatlanul): a szöglet- és
# lapátlagok nagy részben a véletlent tükrözik, ezért a ligaátlag felé húzza őket. A visszamérésből illesztve.
# A gólokra csak akkor vonatkozik, ha nincs csapaterő-modell (rating_model), mert az már jól kalibrált.
SPREAD = {"goals": 0.6, "corners": 0.4, "yellows": 0.6}
# A szóródás a Poisson-eloszláséhoz képest (1 = Poisson): a szögletek és a lapok ennél jobban szórnak.
DISPERSION = {"corners": 1.15, "yellows": 1.1}
DRAW_BOOST = 0.03  # a döntetlen eredmények esélyének növelése (a független Poisson alábecsüli a döntetlent)

# Időjárás-előrejelzés a stadion városára: kulcs nélküli Open-Meteo (helykereső és óránkénti előrejelzés).
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 16  # ennyi napra előre van előrejelzés
GEOCODE_COUNTRIES = {"England": "United Kingdom", "Scotland": "United Kingdom", "Wales": "United Kingdom", "USA": "United States"}
# Az ESPN városneve -> amit a helykereső megtalál (a helyi nevű nagyvárosok helyett különben kis falvakat ad).
GEOCODE_CITIES = {
    "Milano": "Milan",
    "Napoli": "Naples",
    "Roma": "Rome",
    "Firenze": "Florence",
    "Genova": "Genoa",
    "Venezia": "Venice",
    "Villarreal": "Vila-real",
    "La Coruña": "A Coruña",
    "San Sebastian": "Donostia",
    "Bodø/Glimt": "Bodø",
    "Hamburg Norderstedt": "Hamburg",
    "Newcastle-upon-Tyne": "Newcastle upon Tyne",
}
WEATHER_HOURS = 2  # a kezdés órájától ennyi óra átlagát nézi
RAIN_MM = 0.5  # ennyi mm/óra csapadéktól esős a meccs
# Szélsőséges idő: ekkora eső (mm/óra), széllökés (km/h), havazás (cm/óra), illetve ilyen hideg vagy meleg (°C).
EXTREME_RAIN_MM = 4.0
EXTREME_GUST = 70.0
EXTREME_SNOW_CM = 0.5
EXTREME_COLD = -5.0
EXTREME_HEAT = 33.0

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
ESPN_LEAGUES = {"E0": "eng.1", "SP1": "esp.1", "D1": "ger.1", "I1": "ita.1", "F1": "fra.1", "CL": "uefa.champions"}
MATCH_MINUTES = 90
LIVE_MAX_DAYS = 6  # egy forduló legfeljebb ennyi napjára kérdez le élő adatot
LIVE_NAME_MATCH = 0.6  # ekkora névegyezés kell, hogy egy élő meccset a kiírt meccshez kössünk
LIVE_KICKOFF_HOURS = 36  # legfeljebb ennyivel térhet el a két forrás kezdési ideje
SCHEDULE_MONTHS = 3  # a kiírt meccseket ennyi naptári hónapra előre kéri le (a mostanival együtt)
SCHEDULE_BACK_DAYS = 3  # ennyi napra visszamenőleg is a menetrendben marad a meccs, amíg a meccsadatok közé nem kerül
SCHEDULE_NAME_MATCH = 0.85  # ekkora névegyezés kell, hogy a menetrend csapatát a meccsadatok csapatához kössük
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
    "Inter": "Internazionale",
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
    "CL": [
        ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/champions-league/rss.xml"),
        ("Sky Sports", "https://www.skysports.com/rss/12040"),
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
# football-data.co.uk név -> ahogy a sajtó még emlegeti a csapatot (ékezet nélkül, kisbetűvel).
NEWS_ALIASES = {
    "Man United": ["manchester united", "man utd"],
    "Man City": ["manchester city"],
    "Tottenham": ["spurs"],
    "Nott'm Forest": ["forest"],
    "Newcastle": ["magpies"],
    "Ath Madrid": ["atletico", "atleti"],
    "Ath Bilbao": ["athletic"],
    "Barcelona": ["barca"],
    "Sociedad": ["real sociedad"],
    "Vallecano": ["rayo"],
    "Espanol": ["espanyol"],
    "Paris SG": ["psg"],
    "Marseille": ["om"],
    "Lyon": ["ol"],
    "Bayern Munich": ["bayern", "fc bayern"],
    "Dortmund": ["bvb", "borussia dortmund"],
    "M'gladbach": ["gladbach", "monchengladbach"],
    "Ein Frankfurt": ["eintracht frankfurt", "eintracht"],
    "FC Koln": ["koln", "cologne"],
    "Leverkusen": ["bayer 04", "bayer leverkusen"],
    "Milan": ["rossoneri"],
    "Inter": ["nerazzurri"],
    "Juventus": ["juve"],
}
NEWS_MAX_AGE_DAYS = 7  # ennél régebbi cikk nem számít friss hírnek (néhány hírfolyam évekre visszamenő)
SQUAD_NEWS = ("sérülés", "eltiltás", "kezdőcsapat")  # a hiányzókat és kezdőket érintő kategóriák

USUAL_STARTS = 3  # aki a csapat utolsó meccsei közül legalább ennyin kezdett, az szokásos kezdő
USUAL_MIN_GAMES = 3  # ennél kevesebb ismert kezdőcsapatból nem állapít meg szokásos kezdőt

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
    if league in EUROPEAN_CUPS:
        return fetch_matches_espn(league, 2000 + int(season[:2]))
    return parse_matches(download_csv(DATA_URL.format(season=season, league=league)))


def parse_dates(dates: pd.Series) -> pd.Series:
    """A forrásfájl nap/hónap/év dátumai; a régebbi szezonok fájljaiban az év kétjegyű."""
    text = dates.astype("string").str.strip()
    return pd.to_datetime(text, format="%d/%m/%Y", errors="coerce").fillna(
        pd.to_datetime(text, format="%d/%m/%y", errors="coerce")
    )


def parse_matches(raw: pd.DataFrame) -> pd.DataFrame:
    """A forrásfájl soraiból a lejátszott meccsek a modell oszlopneveivel; a sorok indexe a forrásé marad."""
    matches = pd.DataFrame(
        {
            "date": parse_dates(raw["Date"]),
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
    # A meccs előtti szorzókból visszafejtett várható gólok (home_implied / away_implied) a csapaterőhöz.
    result = market_probabilities(raw.loc[matches.index], ODDS_1X2)
    total = market_probabilities(raw.loc[matches.index], ODDS_TOTAL)
    matches["home_implied"], matches["away_implied"] = implied_goals(
        result[0].to_numpy(), result[2].to_numpy(), total[0].to_numpy()
    )
    return matches.astype({"home_goals": int, "away_goals": int})


def market_probabilities(raw: pd.DataFrame, groups: list) -> pd.DataFrame:
    """Az első meglévő oszlopcsoport szorzóiból az árrés nélküli esélyek (oszlopok: 0, 1, ...); szorzó nélkül NaN."""
    result = pd.DataFrame(NAN, index=raw.index, columns=range(len(groups[0])))
    for columns in groups:
        if not set(columns) <= set(raw.columns):
            continue
        inverse = 1 / raw[list(columns)].apply(pd.to_numeric, errors="coerce")
        inverse.columns = result.columns
        result = result.fillna(inverse.div(inverse.sum(axis=1, skipna=False), axis=0))
    return result


def poisson_grid(home_goals: np.ndarray, away_goals: np.ndarray) -> np.ndarray:
    """Az eredmények esélye (meccs × hazai gól × vendég gól) független Poisson-eloszlásokkal."""
    counts = np.arange(MAX_GOALS + 1)
    factorials = np.array([math.lgamma(count + 1) for count in counts])
    home = np.exp(-home_goals[:, None] + counts * np.log(home_goals[:, None]) - factorials)
    away = np.exp(-away_goals[:, None] + counts * np.log(away_goals[:, None]) - factorials)
    return home[:, :, None] * away[:, None, :]


@lru_cache(maxsize=1)
def implied_table() -> tuple:
    """Táblázat a szorzók visszafejtéséhez: össz-gól és hazai rész -> P(hazai) − P(vendég), illetve P(2,5 gól felett)."""
    totals = np.arange(1.2, 5.01, 0.02)
    shares = np.arange(0.08, 0.921, 0.004)
    total, share = np.meshgrid(totals, shares, indexing="ij")
    grid = poisson_grid((total * share).ravel(), (total * (1 - share)).ravel())
    home = np.tril(grid, -1).sum(axis=(1, 2))
    away = np.triu(grid, 1).sum(axis=(1, 2))
    goals = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
    over = grid[:, goals > GOAL_LINE].sum(axis=1)
    return totals, shares, (home - away).reshape(total.shape), over.reshape(total.shape)


def implied_goals(p_home: np.ndarray, p_away: np.ndarray, p_over: np.ndarray) -> tuple:
    """A szorzók esélyeiből visszafejtett várható gólok: (hazai, vendég) tömb; 1X2-szorzó nélkül NaN.

    Azt a várható gólpárt keresi, amellyel a Poisson-modell a piac hazai–vendég esélykülönbségét és a
    2,5 gól feletti esélyét adja. Gólszám-szorzó nélkül az össz-gól IMPLIED_TOTAL.
    """
    totals, shares, difference, over = implied_table()
    known = ~(np.isnan(p_home) | np.isnan(p_away))
    gap = np.where(known, p_home - p_away, 0.0)
    total_index = np.full(len(gap), int(np.searchsorted(totals, IMPLIED_TOTAL)))
    for _ in range(4):
        share_index = np.clip((difference[total_index] < gap[:, None]).sum(axis=1), 0, len(shares) - 1)
        found = np.clip((over[:, share_index].T < p_over[:, None]).sum(axis=1), 0, len(totals) - 1)
        total_index = np.where(np.isnan(p_over), total_index, found)
    total, share = totals[total_index], shares[share_index]
    return np.where(known, total * share, NAN), np.where(known, total * (1 - share), NAN)


def espn_months(league: str, months) -> list:
    """Az ESPN eredményjelzőjének eseményei a megadott hónapokra; amelyik hónap nem tölthető le, kimarad."""

    def one(month) -> list:
        try:
            response = requests.get(
                ESPN_URL.format(league=ESPN_LEAGUES[league]),
                params={"dates": month.strftime("%Y%m"), "limit": 300},
                timeout=30,
            )
            response.raise_for_status()
            return response.json().get("events", [])
        except (requests.RequestException, ValueError):
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        return [event for events in pool.map(one, months) for event in events]


def fetch_matches_espn(league: str, start_year: int) -> pd.DataFrame:
    """Egy nemzetközi kupa szezonjának lejátszott meccsei az ESPN-ről, a fetch_matches oszlopaival.

    A labdabirtoklás (home_possession / away_possession) is benne van. A csapatnevek az ESPN
    nevei; a dátum a budapesti naptári nap. A selejtezők (EUROPEAN_FROM_MONTH előtt) kimaradnak.
    """
    last = min(pd.Period(f"{start_year + 1}-06", "M"), pd.Timestamp.now().to_period("M"))
    names = {"wonCorners": "corners", "totalShots": "shots", "shotsOnTarget": "shots_on_target", "possessionPct": "possession"}
    rows = []
    for event in espn_months(league, pd.period_range(f"{start_year}-{EUROPEAN_FROM_MONTH:02d}", last, freq="M")):
        competition = event["competitions"][0]
        sides = {side["homeAway"]: side for side in competition["competitors"]}
        if set(sides) != {"home", "away"} or not event["status"]["type"].get("completed"):
            continue
        row = {
            "id": event["id"],
            "date": pd.Timestamp(event["date"]).tz_convert("Europe/Budapest").tz_localize(None).normalize(),
            "referee": NAN,
        }
        for where, side in sides.items():
            row[where] = side["team"]["displayName"]
            row[f"{where}_goals"] = int(side.get("score") or 0)
            row[f"{where}_yellows"] = sum(
                1
                for detail in competition.get("details", [])
                if detail.get("yellowCard") and detail.get("team", {}).get("id") == side["team"]["id"]
            )
            values = {entry.get("name"): entry.get("displayValue") for entry in side.get("statistics", [])}
            for source, stat in names.items():
                try:
                    row[f"{where}_{stat}"] = float(values[source])
                except (KeyError, TypeError, ValueError):
                    row[f"{where}_{stat}"] = NAN
        rows.append(row)
    columns = ["date", "home", "away", "referee"] + [
        f"{where}_{stat}"
        for stat in ("goals", "corners", "yellows", "shots", "shots_on_target", "possession")
        for where in ("home", "away")
    ]
    matches = pd.DataFrame(rows).drop_duplicates("id") if rows else pd.DataFrame(columns=["id", *columns])
    matches = matches[columns].astype({"home_goals": int, "away_goals": int, "date": "datetime64[ns]"})
    return matches.sort_values("date", kind="stable").reset_index(drop=True)


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


def news_names(team: str) -> set:
    """A csapat névváltozatai a hírekben való kereséshez (ékezet nélkül, kisbetűvel)."""
    return {plain_name(team), plain_name(LIVE_ALIASES.get(team, team)), *NEWS_ALIASES.get(team, [])}


def team_articles(items: list, team: str) -> list:
    """A csapatot a címében vagy a bevezetőjében említő összes cikk, témától függetlenül, a legújabbtól."""
    names = news_names(team)
    return [
        item
        for item in items
        if any(f" {name} " in f" {plain_name(item['title'] + ' ' + item['summary'])} " for name in names)
    ]


def tag_news(items: list, teams: list) -> list:
    """A meccsek szempontjából fontos cikkek, amelyek a megadott csapatokat említik.

    A cikkek "teams" és "categories" mezőt kapnak; ami nem említ csapatot vagy
    nem illik egyik kategóriába sem (NEWS_KEYWORDS: sérülés, eltiltás,
    kezdőcsapat, edzőváltás, rotáció, sajtótájékoztató), kimarad.
    """
    names = {team: news_names(team) for team in teams}
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
    own_default, opponent_default = model.get("new_team", (1.0, 1.0))  # akiről még nincs adat
    return (
        model["home_avg"]
        * teams["own_strength_home"].get(home, own_default)
        * teams["opponent_strength_away"].get(away, opponent_default),
        model["away_avg"]
        * teams["own_strength_away"].get(away, own_default)
        * teams["opponent_strength_home"].get(home, opponent_default),
    )


def rating_model(history: pd.DataFrame):
    """A gólok csapaterő-modellje a stat_model alakjában, vagy None, ha a meccsekhez nincs elég szorzó.

    Minden csapatnak egy támadó- és egy kapott-ereje van; a várható gól a ligaátlag × a csapat támadóereje ×
    az ellenfél kapott-ereje, így az ellenfelek erőssége is beleszámít. Az erőket a korábbi meccsek
    célértékére illeszti: a meccs előtti szorzókból visszafejtett várható gól (RATING_MARKET súllyal), a
    többi a tényleges gól és az xG keveréke. A friss meccsek többet számítanak (RATING_HALF_LIFE), az
    újonc csapat RATING_NEW_TEAM erőről indul. A history-ban a "weight" oszlop csak az újoncok
    felismeréséhez kell: akinek nincs 1-nél kisebb súlyú (előző szezonbeli) meccse, az újonc.
    """
    if "home_implied" not in history.columns or history.empty:
        return None
    played = history if "home_xg" in history.columns else add_xg(history)
    if played["home_implied"].notna().mean() < RATING_MIN_SHARE:
        return None
    targets = {}
    for side in ("home", "away"):
        goals = played[f"{side}_goals"].astype(float)
        own = (1 - XG_WEIGHT) * goals + XG_WEIGHT * played[f"{side}_xg"].fillna(goals)
        targets[side] = ((1 - RATING_MARKET) * own + RATING_MARKET * played[f"{side}_implied"].fillna(own)).to_numpy(float)
    names = sorted(set(played["home"]) | set(played["away"]))
    index = {team: number for number, team in enumerate(names)}
    home = played["home"].map(index).to_numpy()
    away = played["away"].map(index).to_numpy()
    age = (played["date"].max() - played["date"]).dt.days.to_numpy()
    weight = 0.5 ** (age / RATING_HALF_LIFE)

    earlier = played[played["weight"] < 1]
    veterans = set(earlier["home"]) | set(earlier["away"])
    prior_attack, prior_concede = np.ones(len(names)), np.ones(len(names))
    if veterans:
        newcomers = [index[team] for team in names if team not in veterans]
        prior_attack[newcomers], prior_concede[newcomers] = RATING_NEW_TEAM

    count = len(names)
    attack, concede = np.ones(count), np.ones(count)
    away_avg = (weight * targets["away"]).sum() / weight.sum()
    venue = (weight * targets["home"]).sum() / (weight * targets["away"]).sum()
    prior = RATING_PRIOR * (weight * (targets["home"] + targets["away"])).sum() / (2 * weight.sum())
    for _ in range(25):  # felváltva illeszti a támadó- és a kapott-erőt, majd a ligaátlagot és a hazai pálya előnyét
        scored = np.bincount(home, weight * targets["home"], count) + np.bincount(away, weight * targets["away"], count)
        chances = np.bincount(home, weight * away_avg * venue * concede[away], count) + np.bincount(
            away, weight * away_avg * concede[home], count
        )
        attack = (scored + prior * prior_attack) / (chances + prior)
        let_in = np.bincount(away, weight * targets["home"], count) + np.bincount(home, weight * targets["away"], count)
        chances = np.bincount(away, weight * away_avg * venue * attack[home], count) + np.bincount(
            home, weight * away_avg * attack[away], count
        )
        concede = (let_in + prior * prior_concede) / (chances + prior)
        away_avg = (weight * (targets["home"] + targets["away"])).sum() / (
            weight * (venue * attack[home] * concede[away] + attack[away] * concede[home])
        ).sum()
        venue = (weight * targets["home"]).sum() / (weight * away_avg * attack[home] * concede[away]).sum()

    middle = away_avg * (1 + venue) / 2
    teams = pd.DataFrame(
        {
            "own_avg": middle * attack,
            "opponent_avg": middle * concede,
            "own_strength_home": attack,
            "own_strength_away": attack,
            "opponent_strength_home": concede,
            "opponent_strength_away": concede,
        },
        index=names,
    )
    return {"teams": teams, "home_avg": away_avg * venue, "away_avg": away_avg, "new_team": RATING_NEW_TEAM}


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


def fetch_schedule(league: str, teams: list, months: int = SCHEDULE_MONTHS) -> pd.DataFrame:
    """A bajnokság kiírt meccsei hónapokra előre az ESPN-ről, budapesti idő szerint.

    A fetch_fixtures alakjában adja vissza (kickoff, home, away, referee, plusz venue, city, country: a stadion), a csapatokat a
    megadott football-data.co.uk nevekre fordítva; amelyik meccs csapatát nem tudja
    egyértelműen azonosítani, vagy amelyiket elhalasztották, kimarad. Az elmúlt
    SCHEDULE_BACK_DAYS nap (élő és frissen befejezett) meccsei is benne vannak. Játékvezetőt nem ad.
    """
    now = pd.Timestamp.now(tz="UTC")
    first = now - pd.Timedelta(days=SCHEDULE_BACK_DAYS)

    def one(month) -> list:
        response = requests.get(
            ESPN_URL.format(league=ESPN_LEAGUES[league]), params={"dates": month.strftime("%Y%m"), "limit": 300}, timeout=20
        )
        response.raise_for_status()
        return response.json().get("events", [])

    wanted = pd.period_range(first.tz_localize(None).to_period("M"), now.tz_localize(None).to_period("M") + months - 1)
    with ThreadPoolExecutor(max_workers=6) as pool:  # a hónapok egyszerre töltődnek le; hiba esetén az egész lekérés hibás
        events = {event["id"]: event for month in pool.map(one, wanted) for event in month}

    listed, names = [], {}
    for event in events.values():
        sides = {side["homeAway"]: side["team"] for side in event["competitions"][0]["competitors"]}
        kind = event["status"]["type"]
        kickoff = pd.Timestamp(event["date"])
        # Elhalasztott, félbeszakadt vagy törölt meccs: lezárt, de nincs végeredménye.
        called_off = kind.get("name") == "STATUS_POSTPONED" or (kind.get("state") == "post" and not kind.get("completed"))
        if set(sides) != {"home", "away"} or called_off or kickoff < first:
            continue
        for team in sides.values():
            names[team["id"]] = [team.get(key, "") for key in ("displayName", "shortDisplayName", "name", "location")]
        venue = event["competitions"][0].get("venue") or {}
        address = venue.get("address") or {}
        place = (venue.get("fullName") or "", address.get("city") or "", address.get("country") or "")
        listed.append((kickoff.tz_convert("Europe/Budapest"), sides["home"]["id"], sides["away"]["id"], place))

    known = match_team_names(names, teams)
    rows = [
        {
            "kickoff": kickoff,
            "home": known[home],
            "away": known[away],
            "referee": NAN,
            "venue": place[0],
            "city": place[1],
            "country": place[2],
        }
        for kickoff, home, away, place in listed
        if home in known and away in known
    ]
    schedule = pd.DataFrame(rows, columns=["kickoff", "home", "away", "referee", "venue", "city", "country"])
    # Üres menetrendnél is időbélyeg típusú marad az oszlop.
    schedule["kickoff"] = pd.to_datetime(schedule["kickoff"], utc=True).dt.tz_convert("Europe/Budapest")
    return schedule.sort_values("kickoff", kind="stable").reset_index(drop=True)


def match_team_names(names: dict, teams: list) -> dict:
    """Az ESPN csapatazonosítóihoz (azonosító -> névváltozatok) a football-data.co.uk csapatnév.

    Egy-egy párosítás a legjobb névegyezéstől lefelé, hogy a hasonló nevű csapatok
    (Inter – Milan) ne keveredjenek; akit nem tud biztosan azonosítani, kimarad.
    """
    scores = sorted(
        ((name_score(team, variants), team, team_id) for team_id, variants in names.items() for team in teams),
        key=lambda item: item[0],
        reverse=True,
    )
    known = {}
    for score, team, team_id in scores:
        if score >= SCHEDULE_NAME_MATCH and team_id not in known and team not in known.values():
            known[team_id] = team
    return known


def fetch_possession(league: str, start_year: int, teams: list) -> pd.DataFrame:
    """A szezon lejátszott meccseinek labdabirtoklása az ESPN-ről: home, away, home_possession, away_possession (%).

    A football-data.co.uk fájljaiban nincs labdabirtoklás, ezért a csapatok játékstílusához
    innen jön; a csapatnevek a megadott football-data nevekre fordulnak. Ami nem tölthető le, kimarad.
    """
    last = min(pd.Period(f"{start_year + 1}-06", "M"), pd.Timestamp.now().to_period("M"))

    def one(month) -> list:
        try:
            response = requests.get(
                ESPN_URL.format(league=ESPN_LEAGUES[league]),
                params={"dates": month.strftime("%Y%m"), "limit": 300},
                timeout=30,
            )
            response.raise_for_status()
            return response.json().get("events", [])
        except (requests.RequestException, ValueError):
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        events = [event for month in pool.map(one, pd.period_range(f"{start_year}-08", last, freq="M")) for event in month]

    names, played = {}, []
    for event in events:
        sides = {side["homeAway"]: side for side in event["competitions"][0]["competitors"]}
        if set(sides) != {"home", "away"} or not event["status"]["type"].get("completed"):
            continue
        share = {}
        for where, side in sides.items():
            team = side["team"]
            names[team["id"]] = [team.get(key, "") for key in ("displayName", "shortDisplayName", "name", "location")]
            for entry in side.get("statistics", []):
                if entry.get("name") == "possessionPct":
                    try:
                        share[where] = float(entry.get("displayValue"))
                    except (TypeError, ValueError):
                        pass
        # Hiányzó adatnál a forrás 0-t ad; a két érték összege valódi adatnál 100 körüli.
        if len(share) == 2 and 95 <= sum(share.values()) <= 105 and min(share.values()) > 0:
            played.append((sides["home"]["team"]["id"], sides["away"]["team"]["id"], share["home"], share["away"]))
    known = match_team_names(names, teams)
    rows = [
        {"home": known[home], "away": known[away], "home_possession": home_share, "away_possession": away_share}
        for home, away, home_share, away_share in played
        if home in known and away in known
    ]
    table = pd.DataFrame(rows, columns=["home", "away", "home_possession", "away_possession"])
    return table.drop_duplicates(["home", "away"], keep="last").reset_index(drop=True)


def geocode(city: str, country: str = ""):
    """A város (szélesség, hosszúság) koordinátái, vagy None, ha nem található.

    Több azonos nevű hely közül a megadott országban lévők (ennek híján az összes) legnépesebbjét választja.
    """
    name = GEOCODE_CITIES.get(city, city)
    response = requests.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en"}, timeout=20)
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return None
    wanted = GEOCODE_COUNTRIES.get(country, country)
    places = [place for place in results if place.get("country") == wanted] or results
    best = max(places, key=lambda place: place.get("population") or 0)
    return float(best["latitude"]), float(best["longitude"])


def fetch_forecast(latitude: float, longitude: float) -> pd.DataFrame:
    """Óránkénti előrejelzés a helyre FORECAST_DAYS napra: temperature (°C), rain (mm), wind, gusts (km/h), snow (cm); az index UTC idő."""
    response = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,snowfall",
            "forecast_days": FORECAST_DAYS,
            "timezone": "UTC",
        },
        timeout=20,
    )
    response.raise_for_status()
    hourly = response.json()["hourly"]
    return pd.DataFrame(
        {
            "temperature": hourly["temperature_2m"],
            "rain": hourly["precipitation"],
            "wind": hourly["wind_speed_10m"],
            "gusts": hourly["wind_gusts_10m"],
            "snow": hourly["snowfall"],
        },
        index=pd.to_datetime(hourly["time"], utc=True),
    ).astype(float)


def match_weather(forecast: pd.DataFrame, kickoff):
    """A meccs idejére várható időjárás az előrejelzésből, vagy None, ha arra az időre még nincs előrejelzés.

    Eredmény: {"key" (a WEATHER kulcsa: normal / rain / extreme), "temperature", "rain" (mm/óra), "wind",
    "gusts", "snow", "text" (rövid leírás)}. A kezdés órájától WEATHER_HOURS óra átlagát, a lökéseknél a csúcsát veszi.
    """
    start = pd.Timestamp(kickoff).tz_convert("UTC").floor("h")
    hours = forecast.loc[start : start + pd.Timedelta(hours=WEATHER_HOURS - 1)].dropna()
    if len(hours) < WEATHER_HOURS:
        return None
    weather = {
        "temperature": float(hours["temperature"].mean()),
        "rain": float(hours["rain"].mean()),
        "wind": float(hours["wind"].mean()),
        "gusts": float(hours["gusts"].max()),
        "snow": float(hours["snow"].mean()),
    }
    extreme = (
        weather["rain"] >= EXTREME_RAIN_MM
        or weather["gusts"] >= EXTREME_GUST
        or weather["snow"] >= EXTREME_SNOW_CM
        or not EXTREME_COLD < weather["temperature"] < EXTREME_HEAT
    )
    weather["key"] = "extreme" if extreme else "rain" if weather["rain"] >= RAIN_MM or weather["snow"] > 0 else "normal"
    parts = [f"{weather['temperature']:.0f} °C"]
    if weather["snow"] > 0:
        parts.append(f"hó {number_text(weather['snow'])} cm/óra")
    elif weather["rain"] >= 0.1:
        parts.append(f"eső {number_text(weather['rain'])} mm/óra")
    else:
        parts.append("száraz")
    parts.append(f"szél {weather['wind']:.0f} km/h" + (f" (lökések {weather['gusts']:.0f})" if weather["gusts"] >= 50 else ""))
    weather["text"] = ", ".join(parts)
    return weather


def fetch_upcoming(league: str, teams: list) -> pd.DataFrame:
    """A kiírt meccsek hónapokra előre (fetch_schedule), a következő forduló játékvezetőivel kiegészítve.

    A játékvezető a football-data.co.uk listájából jön, amely csak a következő napokat
    tartalmazza; ha a hosszabb menetrend nem érhető el, ez a rövid lista marad.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:  # a két forrás egyszerre töltődik le
        short, long = pool.submit(fetch_fixtures, league), pool.submit(fetch_schedule, league, teams)
        try:
            near = short.result()
        except (requests.RequestException, KeyError, ValueError):
            near = None
        try:
            schedule = long.result()
        except (requests.RequestException, KeyError, ValueError):
            if near is None:
                raise
            return near
    if near is not None and not near.empty and not schedule.empty:
        referees = near.set_index(["home", "away"])["referee"]
        referees = referees[~referees.index.duplicated()]
        schedule["referee"] = [referees.get(pair, NAN) for pair in zip(schedule["home"], schedule["away"])]
    return schedule


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


def add_possession(matches: pd.DataFrame, possession: pd.DataFrame) -> pd.DataFrame:
    """A meccsekhez hozzáfűzi a labdabirtoklást (home_possession / away_possession); ahol nincs adat, üres marad."""
    return matches.merge(possession, on=["home", "away"], how="left")


def team_styles(history: pd.DataFrame):
    """Csapatonként a játékstílus a meccsekből, vagy None, ha nincs labdabirtoklási adat.

    Oszlopok: possession (%), shots_for, shots_against, corners_for, corners_against (meccsenkénti
    súlyozott átlag), style és attack (high / mid / low: a liga felső, középső, alsó harmada
    labdabirtoklásban, illetve lövésszámban), label (szöveges jellemzés).
    """
    needed = {"home_possession", "away_possession", "home_shots", "away_shots"}
    if not needed <= set(history.columns):
        return None
    sides = []
    for own, other in (("home", "away"), ("away", "home")):
        sides.append(
            pd.DataFrame(
                {
                    "team": history[own],
                    "weight": history["weight"],
                    "possession": history[f"{own}_possession"],
                    "shots_for": history[f"{own}_shots"],
                    "shots_against": history[f"{other}_shots"],
                    "corners_for": history[f"{own}_corners"],
                    "corners_against": history[f"{other}_corners"],
                }
            )
        )
    rows = pd.concat(sides, ignore_index=True).dropna(subset=["possession", "shots_for", "shots_against"])
    if rows.empty:
        return None
    columns = ["possession", "shots_for", "shots_against", "corners_for", "corners_against"]
    weighted = rows[columns].multiply(rows["weight"], axis=0).assign(team=rows["team"], weight=rows["weight"])
    totals = weighted.groupby("team").sum()
    styles = totals[columns].div(totals["weight"], axis=0)
    styles = styles[totals["weight"] >= STYLE_MIN_MATCHES]
    if len(styles) < 6:
        return None

    def third(values: pd.Series) -> pd.Series:
        rank = values.rank(pct=True)
        return pd.Series(np.where(rank > 2 / 3, "high", np.where(rank <= 1 / 3, "low", "mid")), index=values.index)

    styles["style"] = third(styles["possession"])
    styles["attack"] = third(styles["shots_for"])
    styles["label"] = [
        ", ".join(part for part in (STYLE_NAMES[style], ATTACK_NAMES[attack]) if part)
        for style, attack in zip(styles["style"], styles["attack"])
    ]
    return styles


def number_text(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def style_reading(styles, home: str, away: str):
    """A párosítás „emberi" olvasata a két csapat stílusából, vagy None, ha valamelyiké nem ismert.

    Eredmény: {"home", "away" (a csapat stílussora), "key" (stíluspár), "matchup" (a STYLE_MATCHUPS
    sora), "lines" (szöveges magyarázat: a két csapat jellemzése és hogy mit hozott eddig az ilyen párosítás)}.
    """
    if styles is None or home not in styles.index or away not in styles.index:
        return None
    own, other = styles.loc[home], styles.loc[away]
    key = (own["style"], other["style"])
    past = STYLE_MATCHUPS[key]

    def profile(team: str, row) -> str:
        return (
            f"{team}: {row['label']} csapat – {number_text(row['possession'], 0)}% labdabirtoklás, "
            f"{number_text(row['shots_for'])} lövés és {number_text(row['corners_for'])} szöglet meccsenként, "
            f"az ellenfelei {number_text(row['shots_against'])} lövésig jutnak."
        )

    pictures = {
        ("high", "high"): "Két labdatartó csapat: nyílt, sok helyzetet hozó meccs várható, mindkét kapu előtt van esemény.",
        ("high", "mid"): "A hazai csapat birtokolja többet a labdát, és ő irányítja a meccset.",
        ("high", "low"): "A hazai csapat nyom, a vendég visszaáll és kontrákra vár: egykapuzás jellegű meccs, a szögletek zöme a hazaiaké, kevés a lap.",
        ("mid", "high"): "A vendég a labdatartóbb csapat, a hazaiaknak több védekezés jut.",
        ("mid", "mid"): "Két kiegyensúlyozott stílusú csapat, egyik sem enged át tartósan területet.",
        ("mid", "low"): "A vendég visszaáll; a hazaiaknak kell feltörniük a zárt védelmet, ez általában kevesebb gólt hoz.",
        ("low", "high"): "A vendég birtokolja a labdát, a hazai csapat a saját pályáján is visszaáll és kontrázik.",
        ("low", "mid"): "A hazai csapat inkább kivár; lassabb, kevesebb gólos meccs, gyakori a döntetlen.",
        ("low", "low"): "Két visszaálló csapat: egyik sem vállalja a játék irányítását, ezért kevés a gól és gyakori a döntetlen.",
    }
    lines = [
        profile(home, own),
        profile(away, other),
        pictures[key],
        (
            f"Ilyen párosításban ({STYLE_NAMES[key[0]]} hazai – {STYLE_NAMES[key[1]]} vendég) {past['n']} korábbi meccsen: "
            f"átlag {number_text(past['goals'], 2)} gól (2,5 felett {past['over']:.0%}, mindkét csapat gólja {past['btts']:.0%}), "
            f"{number_text(sum(past['corners']))} szöglet ({number_text(past['corners'][0])} hazai – "
            f"{number_text(past['corners'][1])} vendég), {number_text(past['yellows'])} sárga lap, "
            f"hazai győzelem {past['home']:.0%}, döntetlen {past['draw']:.0%}."
        ),
        (
            f"Viszonyításul az összes meccs átlaga: {number_text(STYLE_AVERAGE['goals'], 2)} gól, "
            f"{number_text(STYLE_AVERAGE['corners'])} szöglet, {number_text(STYLE_AVERAGE['yellows'])} sárga lap, "
            f"döntetlen {STYLE_AVERAGE['draw']:.0%}."
        ),
    ]
    return {"home": own, "away": other, "key": key, "matchup": past, "lines": lines}


def build_models(history: pd.DataFrame) -> dict:
    """A gól-, szöglet- és lapmodell, valamint az xG-modell ("xg"; None, ha nincs lövésadat).

    A "derby" kulcs alatt ugyanezek rangadóra: ott a friss forma csak DERBY_FORM_SHARE súllyal számít.
    """
    history = add_xg(history)
    stats = (*STATS, "xg")
    models = {stat: stat_model(history, stat) for stat in stats}
    models["derby"] = {stat: stat_model(history, stat, FORM_WEIGHT * DERBY_FORM_SHARE) for stat in stats}
    # A gólok csapaterő-modellje a friss meccseket magától súlyozza, ezért rangadón is ugyanaz.
    models["rating"] = models["derby"]["rating"] = rating_model(history)
    models["styles"] = team_styles(history)
    return models


def domestic_strengths(domestic: dict) -> dict:
    """A hazai bajnokságok csapatainak ereje: (liga, csapat) -> {"goals": (támadás, kapott), "corners": (...)}.

    A domestic liga -> súlyozott meccsek (mint a build_models bemenete). Az erő a csapat átlaga a
    ligaátlaghoz képest (1 = átlagos); a gólnál az xG XG_WEIGHT súllyal számít, ahogy a bajnoki modellben.
    """
    strengths = {}
    for league, history in domestic.items():
        history = add_xg(history)
        relative = {}
        for stat in ("goals", "xg", "corners"):
            model = stat_model(history, stat)
            if model is not None:
                average = (model["home_avg"] + model["away_avg"]) / 2
                relative[stat] = (model["teams"]["own_avg"] / average, model["teams"]["opponent_avg"] / average)
        if "goals" not in relative:
            continue
        chances = relative.get("xg", relative["goals"])
        for team in relative["goals"][0].index:
            goals = tuple(
                (1 - XG_WEIGHT) * relative["goals"][side][team] + XG_WEIGHT * chances[side].get(team, relative["goals"][side][team])
                for side in (0, 1)
            )
            corners = tuple(relative["corners"][side].get(team, 1.0) for side in (0, 1)) if "corners" in relative else (1.0, 1.0)
            strengths[(league, team)] = {"goals": goals, "corners": corners}
    return strengths


def cup_team_leagues(teams: list, strengths: dict) -> dict:
    """A kupa csapataihoz (ESPN-név) a hazai bajnoki megfelelőjük: név -> (liga, football-data név); akit nem talál, kimarad."""
    known = {}
    for team in teams:
        best, best_score = None, SCHEDULE_NAME_MATCH
        for league, name in strengths:
            score = 1.0 if CUP_NAME_ALIASES.get(name) == team else name_score(name, [team])
            if score > best_score or (score == best_score and best is None):
                best, best_score = (league, name), score
        if best is not None:
            known[team] = best
    return known


def cup_stat_model(history: pd.DataFrame, stat: str, leagues: dict, strengths: dict) -> dict:
    """Egy kupa gól- vagy szögletmodellje a stat_model alakjában, a csapatok hazai bajnoki erejéből (lásd CUP_MODEL).

    A hazai bajnoki alapú becslést a csapat friss kupameccsei módosítják: a tényleges és a várt érték
    aránya, CUP_PRIOR_MATCHES meccsnyi súllyal a ligaerősségből számolt érték felé húzva.
    """
    settings = CUP_MODEL[stat]
    teams = sorted(set(history["home"]) | set(history["away"]) | set(leagues))

    def strength(team: str) -> tuple:
        league, name = leagues.get(team, ("NONE", None))
        attack, concede = strengths[(league, name)][stat] if name else (1.0, 1.0)
        league_attack, league_concede = settings["leagues"][CUP_LEAGUE_GROUP.get(league, league)]
        return (
            max(attack, 0.05) ** settings["attack_power"] * league_attack,
            max(concede, 0.05) ** settings["defence_power"] * league_concede,
        )

    base = {team: strength(team) for team in teams}
    # A friss kupameccsek: mennyit szerzett és kapott a csapat ahhoz képest, amit a bajnoki ereje alapján vártunk.
    recent = history.dropna(subset=[f"home_{stat}", f"away_{stat}"])
    if not recent.empty:
        recent = recent[recent["date"] >= recent["date"].max() - pd.Timedelta(days=CUP_UPDATE_DAYS)]
    prior = CUP_PRIOR_MATCHES * settings["base"] * (1 + settings["home"]) / 2
    scored = {team: [prior, prior] for team in teams}  # tényleges, várt szerzett
    conceded = {team: [prior, prior] for team in teams}
    for row in recent.itertuples():
        for team, other, value, venue in (
            (row.home, row.away, getattr(row, f"home_{stat}"), settings["home"]),
            (row.away, row.home, getattr(row, f"away_{stat}"), 1.0),
        ):
            expected_value = settings["base"] * venue * base[team][0] * base[other][1]
            scored[team][0] += row.weight * value
            scored[team][1] += row.weight * expected_value
            conceded[other][0] += row.weight * value
            conceded[other][1] += row.weight * expected_value
    table = pd.DataFrame(
        {
            "attack": {team: base[team][0] * scored[team][0] / scored[team][1] for team in teams},
            "concede": {team: base[team][1] * conceded[team][0] / conceded[team][1] for team in teams},
        }
    )
    middle = settings["base"] * (1 + settings["home"]) / 2
    return {
        "teams": pd.DataFrame(
            {
                "own_avg": middle * table["attack"],
                "opponent_avg": middle * table["concede"],
                "own_strength_home": table["attack"],
                "own_strength_away": table["attack"],
                "opponent_strength_home": table["concede"],
                "opponent_strength_away": table["concede"],
            }
        ),
        "home_avg": settings["base"] * settings["home"],
        "away_avg": settings["base"],
        "calibrated": True,  # mért összefüggésből jön, a ligaátlag felé húzás (SPREAD) nem kell rá
    }


def build_cup_models(history: pd.DataFrame, domestic: dict) -> dict:
    """Egy nemzetközi kupa modelljei: a gól és a szöglet a csapatok hazai bajnoki erejéből és a ligák erősségéből.

    A history a kupa súlyozott meccsei, a domestic a hazai bajnokságoké (liga -> meccsek). A sárga lap
    és a játékstílus továbbra is a kupameccsekből jön. A "leagues" kulcs alatt a csapatok bajnoksága áll.
    """
    models = build_models(history)
    strengths = domestic_strengths(domestic)
    leagues = cup_team_leagues(sorted(set(history["home"]) | set(history["away"])), strengths)
    for group in (models, models["derby"]):
        for stat in CUP_MODEL:
            group[stat] = cup_stat_model(history, stat, leagues, strengths)
        group["xg"] = None  # az xG már benne van a hazai bajnoki erőben
    models["leagues"] = leagues
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


def apply_h2h(values: dict, models: dict, matches: pd.DataFrame, home: str, away: str, stats=tuple(STATS)) -> dict:
    """A megadott statisztikák várható értékét a két csapat utolsó H2H_MATCHES egymás elleni meccsének átlaga felé húzza.

    A súly a meglévő meccsek arányában nő H2H_WEIGHT-ig, így egy-két találkozó
    csak keveset számít. A fordított pályaválasztású meccsek értékeit a liga
    hazai/vendég átlagának arányával számolja át a mostani felállásra.
    """
    pair = h2h_meetings(matches, home, away)
    blended = dict(values)
    for stat in stats:
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


def predict(home_xg: float, away_xg: float, draw_boost: float = 0.0) -> dict:
    """A két várható gólszámból kiszámolja a gólpiacok kimeneteleinek valószínűségét.

    A draw_boost a döntetlen eredmények esélyét növeli arányosan (a többit ennek megfelelően csökkenti).
    """
    probs = {"1": 0.0, "X": 0.0, "2": 0.0, "over": 0.0, "btts": 0.0}
    home_chances = [poisson(home_xg, goals) for goals in range(MAX_GOALS + 1)]
    away_chances = [poisson(away_xg, goals) for goals in range(MAX_GOALS + 1)]
    level = sum(home * away for home, away in zip(home_chances, away_chances))
    scale = 1 / (1 + draw_boost * level)
    for home_goals in range(MAX_GOALS + 1):
        for away_goals in range(MAX_GOALS + 1):
            p = home_chances[home_goals] * away_chances[away_goals] * scale
            if home_goals == away_goals:
                p *= 1 + draw_boost
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


def count_chance(expected_value: float, k: int, dispersion: float = 1.0) -> float:
    """Egy darabszám esélye: Poisson, 1 feletti szóródásnál (szórásnégyzet / átlag) negatív binomiális."""
    if dispersion <= 1.001:
        return poisson(expected_value, k)
    size = expected_value / (dispersion - 1)
    share = size / (size + expected_value)
    return math.exp(
        math.lgamma(k + size) - math.lgamma(size) - math.lgamma(k + 1) + size * math.log(share) + k * math.log(1 - share)
    )


def over_under(expected_total: float, line: float, dispersion: float = 1.0) -> dict:
    """Több/Kevesebb esély egy darabszám összegére (gól, szöglet, lap); lásd count_chance."""
    under = sum(count_chance(expected_total, k, dispersion) for k in range(int(line) + 1))
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


def build_markets(values: dict, corner_line: float, card_line: float, draw_boost: float = 0.0) -> list:
    """A várható értékekből (stat -> (hazai, vendég)) a piacok és esélyeik."""
    probs = predict(*values["goals"], draw_boost)
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
                else over_under(home_value + away_value, line, DISPERSION[stat]),
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

    A várható gól a csapaterő-modellből jön (rating_model); ennek híján a gól- és az xG-modell
    keveréke (az xG nagyobb súllyal). A szögletet és a lapot – csapaterő-modell nélkül a gólt is – az
    egymás elleni meccsek (a h2h táblából, ennek híján a history-ból) finomítják.
    Erre jönnek a szorzók: hiányzók, játékvezető, és a context-ből (lásd
    match_context) az időjárás, a rangadó, a pihenőnapok, a közeli kupameccs, az
    új edző és a motiváció. Rangadón a forma kisebb súlyú modelljei számolnak.
    """
    home, away = match.home, match.away
    context = context or {}
    derby = bool(context.get("derby"))
    reading = style_reading(models.get("styles"), home, away)
    if derby and "derby" in models:
        models = models["derby"]
    base = {stat: expected(models[stat], home, away) for stat in STATS}
    rated = models.get("rating") is not None
    if rated:
        # A csapaterő-modell várható góljai; az arányukat RATING_STRETCH széthúzza, az összegük marad.
        home_goals, away_goals = expected(models["rating"], home, away)
        ratio = (home_goals / away_goals) ** RATING_STRETCH
        base["goals"] = ((home_goals + away_goals) * ratio / (1 + ratio), (home_goals + away_goals) / (1 + ratio))
    elif models.get("xg") is not None:
        chances = expected(models["xg"], home, away)
        if not any(math.isnan(value) for value in chances):
            base["goals"] = tuple(
                (1 - XG_WEIGHT) * goals + XG_WEIGHT * chance for goals, chance in zip(base["goals"], chances)
            )
    before_h2h = dict(base)
    # A csapaterő-modell góljain az egymás elleni meccsek a visszamérés szerint csak rontanak, ezért ott kimaradnak.
    h2h_stats = tuple(stat for stat in STATS if not (rated and stat == "goals"))
    base = apply_h2h(base, models, history if h2h is None else h2h, home, away, h2h_stats)
    # A csapatok közti különbséget a ligaátlag felé húzza (SPREAD), majd jön a párosítás stílusából adódó
    # korrekció: amit két ilyen jellegű csapat meccse a puszta átlagokhoz képest hozni szokott.
    raw = dict(base)
    style_goals, style_cards, style_draw = STYLE_EFFECT.get(reading["key"], (1.0, 1.0, 0.0)) if reading else (1.0, 1.0, 0.0)
    for stat, style_factor in (("goals", style_goals), ("corners", 1.0), ("yellows", style_cards)):
        total = sum(base[stat])
        if models[stat] is None or math.isnan(total) or total <= 0:
            continue
        league_total = models[stat]["home_avg"] + models[stat]["away_avg"]
        spread = 1.0 if models[stat].get("calibrated") or (rated and stat == "goals") else SPREAD[stat]
        factor = (league_total + spread * (total - league_total)) / total * style_factor
        base[stat] = tuple(value * factor for value in base[stat])
    draw_boost = DRAW_BOOST + style_draw

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

    markets = build_markets(adjusted, corner_line, card_line, draw_boost)
    for market, before in zip(markets, build_markets(base, corner_line, card_line, draw_boost)):
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
    return {"markets": markets, "notes": notes, "key_absences": key_absences, "style": reading, "raw": raw, "before_h2h": before_h2h}


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
    print(f"A szögletek és lapok várható értékében az utolsó {H2H_MATCHES} egymás elleni meccs legfeljebb {H2H_WEIGHT:.0%} súllyal számít.\n")
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
