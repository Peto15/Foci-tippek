"""Webes felület a futball-elemzőhöz.

Indítás:
    streamlit run app.py
"""

import json
import math
import re
import time
from html import escape, unescape
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

import main as model

POSITIONS = {"Kapus": "G", "Védő": "D", "Középpályás": "M", "Támadó": "F"}
REASONS = ["sérült", "eltiltott", "egyéb"]

ODDS_PREFIX = "odds|"
# A beírt szorzók naplója: innen töltődnek vissza újraindítás után, és ebből látszik a szorzók mozgása.
ODDS_LOG = Path(__file__).with_name("odds_log.json")
# Az élő forrás állapotnevei a nem lejátszott meccsekre.
STATUS_NOTES = {
    "Abandoned": "Félbeszakadt",
    "Postponed": "Elhalasztva",
    "Canceled": "Törölve",
    "Suspended": "Felfüggesztve",
    "Delayed": "Késik",
}
LINEUP_WINDOW = pd.Timedelta(minutes=120)  # a kezdés előtt ennyivel kezdi figyelni a kihirdetett kezdőket
POSITION_NAMES = {"G": "Kapus", "D": "Védők", "M": "Középpályások", "F": "Támadók"}
# Hírkategória -> (jel, mit jelent a meccs kimenetelére és a szorzókra nézve).
NEWS_IMPACT = {
    "sérülés": (
        "🩹",
        "ha kulcsjátékost érint, a csapat várható gólja csökken, a kapott gólé nő, és a piac jellemzően emeli a "
        "csapat szorzóját. A modell akkor számol vele, ha a játékost felveszed a hiányzók közé.",
    ),
    "eltiltás": (
        "🟥",
        "biztos hiányzó, a hatása ugyanaz, mint a sérülésé – érdemes rögtön felvenni a hiányzók közé.",
    ),
    "kezdőcsapat": (
        "📋",
        "a kihirdetett kezdőből hiányzó szokásos kezdőket a modell a kezdés előtti két órában magától beszámítja.",
    ),
    "edzőváltás": (
        "🔄",
        "új edzővel a korábbi forma kevésbé irányadó, a becslés bizonytalanabb. A modell ezt nem számolja, "
        "ezért ennél a meccsnél a 70% feletti tippet is érdemes óvatosabban kezelni.",
    ),
    "rotáció": (
        "♻️",
        "pihentetett kezdőkkel a csapat gyengébb; a modell a kihirdetett kezdőből veszi észre, addig kézzel "
        "adható meg hiányzóként (kérdésesnek jelölve fél súllyal számít).",
    ),
    "sajtótájékoztató": (
        "🎙️",
        "az edzői nyilatkozat gyakran előre jelzi a hiányzókat és a rotációt; önmagában nem módosít az esélyeken.",
    ),
}
WEATHER_KEYS = {label: key for key, (label, _, _) in model.WEATHER.items()}

# Az alapszínek a .streamlit/config.toml témájából jönnek; ez a kártyákat, füleket és gombokat formázza.
STYLE = """
<style>
:root {
    --panel: #18201c; --panel-2: #1f2924; --inset: #121916; --line: #2b3832;
    --green: #1fd17f; --green-dim: #12804f; --ink: #06140d;
    --text: #e9f0ec; --muted: #8ea198; --gold: #f2c94c; --red: #ff4d5a;
}
.block-container { padding-top: 4.5rem; max-width: 1400px; }
[data-testid="stSidebar"] { background: #0c100e; border-right: 1px solid var(--line); }

/* Fejléc */
.hero {
    background: linear-gradient(120deg, rgba(31, 209, 127, .20), rgba(31, 209, 127, .03) 60%);
    border: 1px solid var(--line); border-left: 6px solid var(--green);
    border-radius: 20px; padding: 1.2rem 1.5rem; margin-bottom: 1.2rem;
}
.hero-title { font-size: 1.9rem; font-weight: 800; line-height: 1.2; color: var(--text); }
.hero-title span { color: var(--green); }
.hero-sub { color: var(--muted); font-size: .9rem; margin-top: .35rem; }

/* Fülek: pirula alakú gombsor */
.stTabs [data-baseweb="tab-list"] {
    gap: .35rem; padding: .35rem; width: fit-content; max-width: 100%;
    background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
}
.stTabs [data-baseweb="tab"] {
    height: auto; padding: .45rem 1.15rem; border-radius: 999px;
    color: var(--muted); font-weight: 600; transition: background .15s, color .15s;
}
.stTabs [data-baseweb="tab"]:hover { background: var(--panel-2); color: var(--text); }
.stTabs [data-baseweb="tab"][aria-selected="true"],
.stTabs [data-baseweb="tab"][aria-selected="true"] p { background: var(--green); color: var(--ink); }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }

/* Gombok */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    background: linear-gradient(180deg, #25db88, #15a965); color: var(--ink);
    border: 1px solid var(--green-dim); border-radius: 999px;
    font-weight: 700; padding: .5rem 1.4rem;
    transition: transform .12s, box-shadow .12s, filter .12s;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
    color: var(--ink); border-color: var(--green); filter: brightness(1.08);
    transform: translateY(-1px); box-shadow: 0 6px 16px rgba(31, 209, 127, .25);
}
.stButton > button:active { transform: translateY(0); box-shadow: none; }
[data-testid="stNumberInput"] button { color: var(--muted); }
[data-testid="stNumberInput"] button:hover { background: var(--green); color: var(--ink); }

/* Mezők, lenyíló panelek, táblázatok */
[data-baseweb="select"] > div, [data-testid="stNumberInputContainer"] {
    background: var(--panel); border-color: var(--line); border-radius: 12px;
}
[data-testid="stWidgetLabel"] p { color: var(--muted); font-size: .82rem; }
[data-testid="stExpander"] details {
    background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
}
[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }

/* Meccskártyák */
.match-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }
.match-card {
    background: linear-gradient(160deg, #1d2722, #161d1a);
    border: 1px solid var(--line); border-radius: 18px; padding: 1rem 1.1rem;
    box-shadow: 0 6px 18px rgba(0, 0, 0, .25); transition: border-color .15s, transform .15s;
}
.match-card:hover { border-color: var(--green-dim); transform: translateY(-2px); }
.match-meta {
    display: flex; justify-content: space-between; align-items: center;
    color: var(--muted); font-size: .76rem; letter-spacing: .05em; text-transform: uppercase;
}
.score {
    background: var(--inset); border: 1px solid var(--line); border-radius: 8px;
    padding: .1rem .55rem; color: var(--text); font-size: .9rem; font-weight: 700;
}
.match-teams { font-size: 1.12rem; font-weight: 700; margin: .45rem 0 .85rem; color: var(--text); }
.match-teams .vs { color: var(--muted); font-weight: 400; margin: 0 .4rem; }
.match-note { margin-top: .7rem; font-size: .78rem; color: var(--gold); }

/* Élő és befejezett meccsek */
.section {
    display: flex; align-items: center; gap: .6rem; margin: 1.4rem 0 .8rem;
    color: var(--muted); font-size: .8rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
}
.section::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.status { border-radius: 999px; padding: .12rem .6rem; font-size: .72rem; font-weight: 800; letter-spacing: .05em; }
.status.live { background: var(--red); color: #fff; animation: pulse 1.6s ease-in-out infinite; }
.status.done { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); }
.status.off { background: transparent; color: var(--gold); border: 1px solid var(--gold); }
@keyframes pulse { 50% { opacity: .55; } }
.match-card.live { border-color: var(--red); }
.match-teams .result { color: var(--green); margin: 0 .5rem; }
.live-line { color: var(--muted); font-size: .8rem; margin: -.45rem 0 .8rem; }
.mark { display: inline-block; width: 1.25rem; font-weight: 900; }
.mark.win { color: var(--green); }
.mark.lose { color: var(--red); }

/* Tippsorok a meccskártyán: tipp | szorzó | modell */
.picks { display: grid; grid-template-columns: 1fr auto auto; gap: .4rem .55rem; align-items: center; }
.picks .head { color: var(--muted); font-size: .68rem; letter-spacing: .05em; text-transform: uppercase; text-align: right; }
.picks .head:first-child { text-align: left; }
.pick-name { font-weight: 700; color: var(--text); font-size: .92rem; }
.pick-name small { display: block; color: var(--muted); font-weight: 500; font-size: .74rem; }
.pick-name .tag {
    display: inline-block; margin: .2rem .3rem 0 0; padding: .05rem .5rem; border-radius: 999px;
    background: var(--green); color: var(--ink); font-size: .64rem; font-weight: 800; letter-spacing: .05em;
}
.pick-name .tag.risk { background: var(--red); color: #fff; }
.cell {
    min-width: 3.4rem; text-align: center; padding: .3rem .45rem; border-radius: 10px;
    background: var(--inset); border: 1px solid var(--line); color: var(--text); font-weight: 700; font-size: .88rem;
}
.cell.blank { background: transparent; border-color: transparent; }
.cell.prob.top { background: var(--green); border-color: var(--green); color: var(--ink); }
</style>
"""

st.set_page_config(page_title="Foci tippek", page_icon="⚽", layout="wide")
st.html(STYLE)

# A Streamlit eldobja az éppen nem látszó mezők értékét; így a beírt szorzók meccsváltáskor is megmaradnak.
for state_key in list(st.session_state):
    if isinstance(state_key, str) and state_key.startswith(ODDS_PREFIX):
        st.session_state[state_key] = st.session_state[state_key]


@st.cache_data(ttl=3600, show_spinner="Meccsek letöltése…")
def load_season(league: str, start_year: int) -> pd.DataFrame:
    return model.fetch_matches(league, model.season_code(start_year))


@st.cache_data(ttl=900, show_spinner="Kiírt meccsek letöltése…")
def load_fixtures(league: str) -> pd.DataFrame:
    return model.fetch_fixtures(league)


@st.cache_data(ttl=60, show_spinner="Élő adatok letöltése…")
def load_live(league: str, first_day: str, last_day: str) -> tuple:
    """Az élő forrás eseményei és a letöltés időpontja; egy percig gyorsítótárból jön."""
    events = model.fetch_live(league, pd.Timestamp(first_day), pd.Timestamp(last_day))
    return events, pd.Timestamp.now(tz="Europe/Budapest")


@st.cache_data(ttl=900, show_spinner="Szorzók letöltése…")
def load_odds(league: str, bookmaker_keys: tuple, api_key: str) -> tuple:
    """Az iroda szorzói és a letöltés időpontja; negyedóráig gyorsítótárból jön, hogy kímélje a havi keretet."""
    events, remaining = model.fetch_odds(league, bookmaker_keys, api_key)
    return events, remaining, pd.Timestamp.now(tz="Europe/Budapest")


@st.cache_data(ttl=6 * 3600, show_spinner="Nemzetközi kupameccsek letöltése…")
def load_cups(first_day: str, last_day: str) -> list:
    return model.fetch_cup_matches(pd.Timestamp(first_day), pd.Timestamp(last_day))


@st.cache_data(ttl=300, show_spinner=False)
def load_lineup(league: str, event_id: str):
    return model.fetch_lineup_espn(league, event_id)


@st.cache_data(ttl=6 * 3600, show_spinner="Szokásos kezdők megállapítása az előző meccsekből…")
def load_usual(league: str, event_id: str, side: str) -> dict:
    """A meccs egyik csapatának (side: "home" / "away") szokásos kezdői az előző meccsei alapján."""
    return model.usual_starters(league, load_lineup(league, event_id)[side], load_lineup)


@st.cache_data(ttl=900, show_spinner="Hírek letöltése…")
def load_news(league: str) -> tuple:
    return model.fetch_news(league)


def snippet(text: str, length: int = 200) -> str:
    """Egy cikk bevezetője címkék nélkül, a megadott hosszra vágva."""
    plain = " ".join(unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    return plain if len(plain) <= length else plain[:length].rsplit(" ", 1)[0] + "…"


def crumb(item: dict) -> str:
    """Egy hír egy sorban: jelek, forrás, idő, linkelt cím, alatta a bevezető."""
    icons = "".join(NEWS_IMPACT[category][0] for category in item["categories"])
    when = f" · {item['published']:%m.%d. %H:%M}" if item["published"] is not None else ""
    title = item["title"].replace("[", "(").replace("]", ")")
    lead = snippet(item["summary"])
    return f"- {icons} **{item['source']}**{when} — [{title}]({item['link']})" + (f"  \n  {lead}" if lead else "")


def add_absences(news: dict, pair: tuple, side: str, absences: list) -> None:
    """Hiányzók hozzáadása egy meccs egyik oldalához; akit már felvettünk, nem kerül be kétszer."""
    known = {absence["name"] for absence in news[pair][side]}
    news[pair][side] += [absence for absence in absences if absence["name"] not in known]


def kickoff_text(kickoff: pd.Timestamp) -> str:
    text = f"{model.WEEKDAYS[kickoff.weekday()]} {kickoff:%m.%d.}"
    return text if kickoff.tzinfo is None else f"{text} {kickoff:%H:%M}"


def build_news(absences: pd.DataFrame, fixtures: pd.DataFrame) -> dict:
    """A szerkesztett hiányzó-táblázatból a modellnek való csapathírek."""
    by_team = {}
    for row in absences.to_dict("records"):
        team = row.get("Csapat")
        if not isinstance(team, str):
            continue
        name = row.get("Játékos")
        by_team.setdefault(team, []).append(
            {
                "player": {
                    "name": name if isinstance(name, str) and name.strip() else "névtelen",
                    "position": POSITIONS.get(row.get("Poszt"), "M"),
                },
                "type": "doubtful" if row.get("Kérdéses") is True else "missing",
                "reason": row.get("Ok") if isinstance(row.get("Ok"), str) else "hiányzik",
                "key": row.get("Kulcsjátékos") is not False,
                "star": row.get("Kulcsember") is True,
            }
        )
    return {
        (match.home, match.away): {
            "referee": None,
            "home": model.parse_absences({"missingPlayers": by_team.get(match.home, [])}, set(), False),
            "away": model.parse_absences({"missingPlayers": by_team.get(match.away, [])}, set(), False),
        }
        for match in fixtures.itertuples()
    }


def load_odds_log() -> dict:
    try:
        return json.loads(ODDS_LOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def movement_text(movement) -> str:
    if not movement:
        return "–"
    return f"{movement['opening']:.2f} → {movement['current']:.2f} ({movement['change']:+.1%})"


def percent(value):
    return None if value is None or math.isnan(value) else round(value * 100)


# --- Oldalsáv: bajnokság, szezon, nap, határok --------------------------------

current_start = model.current_season_start()
with st.sidebar:
    st.header("Beállítások")
    league = st.selectbox("Bajnokság", list(model.LEAGUES), format_func=model.LEAGUES.get)
    season_start = st.selectbox(
        "Szezon",
        [current_start - offset for offset in range(3)],
        format_func=lambda year: f"{year}/{(year + 1) % 100:02d}",
    )

try:
    season = load_season(league, season_start)
    previous = load_season(league, season_start - 1)
    upcoming = load_fixtures(league) if season_start == current_start else None
    older = model.older_seasons(league, season_start, load_season)
except requests.RequestException as error:
    st.error(f"Nem sikerült letölteni az adatokat: {error}")
    st.stop()

# A meccsek naptári napok szerint választhatók: elöl a kiírt napok (budapesti idő szerint),
# utánuk a szezon lejátszott napjai a legfrissebbtől visszafelé.
upcoming_day = None if upcoming is None else upcoming["kickoff"].dt.tz_localize(None).dt.normalize()
played_day = season["date"].dt.normalize()
day_counts, upcoming_days = {}, []
if upcoming_day is not None:
    for day, count in upcoming_day.value_counts().sort_index().items():
        day_counts[f"{day:%Y-%m-%d}"] = count
        upcoming_days.append(f"{day:%Y-%m-%d}")
for day, count in played_day.value_counts().sort_index(ascending=False).items():
    day_counts.setdefault(f"{day:%Y-%m-%d}", count)


def day_label(option: str) -> str:
    day = pd.Timestamp(option)
    listed = " · kiírt" if option in upcoming_days else ""
    return f"{day:%Y.%m.%d.} ({model.WEEKDAYS[day.weekday()]}) · {day_counts[option]} meccs{listed}"


with st.sidebar:
    if not day_counts:
        st.info("Ebben a szezonban még nincs meccs.")
        st.stop()
    day_options = list(day_counts)
    today = f"{pd.Timestamp.now(tz='Europe/Budapest'):%Y-%m-%d}"
    selected_day = st.selectbox(
        "Nap",
        day_options,
        index=day_options.index(today) if today in upcoming_days else 0,
        format_func=day_label,
    )
    bookmaker = st.selectbox("Fogadóiroda", model.BOOKMAKERS)
    odds_api_key = st.text_input(
        "The Odds API-kulcs",
        value=model.odds_api_key(),
        type="password",
        help="Kulccsal a Tippmixpro és az Unibet oszlopba az Unibet szorzói automatikusan betöltődnek "
        "(the-odds-api.com). A kulcs a projektmappa .env fájljából töltődik be (ODDS_API_KEY=...).",
    ).strip()
    st.divider()
    corner_line =st.number_input("Szöglethatár", 0.5, 20.5, model.CORNER_LINE, step=1.0)
    card_line = st.number_input("Sárgalap-határ", 0.5, 12.5, model.CARD_LINE, step=1.0)
    st.caption("A kiírt napok budapesti idő szerint, a lejátszott napok a meccsadatok dátuma szerint szerepelnek.")

# A modell mindig csak a kiválasztott nap előtti meccsekből számol, így a tipp összevethető az eredménnyel.
selected_date = pd.Timestamp(selected_day)
is_past = selected_day not in upcoming_days
if is_past:
    played = season[played_day == selected_date].sort_values("date", kind="stable")
    fixtures = played.rename(columns={"date": "kickoff"}).reset_index(drop=True)
else:
    fixtures = upcoming[upcoming_day == selected_date].reset_index(drop=True)
season_history = season[played_day < selected_date]

history = pd.concat(
    [previous.assign(weight=model.PREV_SEASON_WEIGHT), season_history.assign(weight=1.0)],
    ignore_index=True,
)
models = model.build_models(history)
form = model.team_form(history)
table = model.build_table(season_history) if not season_history.empty else None
# Az egymás elleni mérleghez régebbi szezonok is kellenek; itt is csak a kiválasztott nap előtti meccsek.
h2h = pd.concat([*older, previous, season_history], ignore_index=True)
teams = sorted(set(fixtures["home"]) | set(fixtures["away"]))

# --- Fejléc -------------------------------------------------------------------

round_text = f"{selected_date:%Y.%m.%d.} ({model.WEEKDAYS[selected_date.weekday()]})"
st.html(
    f'<div class="hero"><div class="hero-title">⚽ {escape(model.LEAGUES[league])} <span>tippek</span></div>'
    f'<div class="hero-sub">{season_start}/{(season_start + 1) % 100:02d}, {escape(round_text)} · '
    f"{len(fixtures)} meccs · a becslés {len(season_history)} idei és {len(previous)} tavalyi meccsre épül"
    "</div></div>"
)

# --- Élő és befejezett meccsek ------------------------------------------------

refresh_column, live_column = st.columns([1, 3], vertical_alignment="center")
if refresh_column.button("🔄 Frissítés (Élő adatok)"):
    load_live.clear()

# Meccsenként az állás: {"state": "upcoming" | "live" | "finished", "goals", "corners", "yellows", ...}.
scores = {}
if is_past:
    for match in fixtures.itertuples():
        scores[(match.home, match.away)] = {
            "state": "finished",
            "goals": (match.home_goals, match.away_goals),
            "corners": (match.home_corners, match.away_corners),
            "yellows": (match.home_yellows, match.away_yellows),
        }
    live_column.caption("Lejátszott nap: a végeredmények a letöltött meccsadatokból jönnek.")
else:
    kickoff_days = fixtures["kickoff"].dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
    try:
        events, fetched = load_live(league, f"{kickoff_days.min():%Y-%m-%d}", f"{kickoff_days.max():%Y-%m-%d}")
        scores = model.match_live(fixtures, events)
        states = [score["state"] for score in scores.values()]
        live_column.caption(
            f"Élő adatok (ESPN), frissítve {fetched:%H:%M:%S}: {states.count('live')} élő, "
            f"{states.count('finished')} befejezett, {len(fixtures) - states.count('live') - states.count('finished')} "
            f"hátravan. Az adat legfeljebb egy percig gyorsítótárból jön; a gomb azonnal újratölti."
        )
    except (requests.RequestException, ValueError, KeyError) as error:
        live_column.caption(f"Az élő adatok most nem érhetők el ({error}); a tippek ettől függetlenül működnek.")

# --- Fülek --------------------------------------------------------------------

tips_tab, stats_tab, info_tab, news_tab = st.tabs(
    [
        "🎯 Ajánlott Tippek",
        "📊 Statisztikák & Várható Mutatók",
        "ℹ️ Egyéb Meccsinformációk",
        "📰 Hírek & Friss Fejlemények",
    ]
)

# --- 3. fül, bevitel: körülmények és hiányzók (a számolás előtt kellenek) -----

names = [f"{match.home} – {match.away}" for match in fixtures.itertuples()]
# A bajnoki előtti és utáni napok nemzetközi kupameccsei a terheltséghez és a rotációhoz.
cup_window = pd.Timedelta(days=model.CUP_WINDOW)
cups = load_cups(f"{selected_date - cup_window:%Y-%m-%d}", f"{selected_date + cup_window:%Y-%m-%d}")
contexts = {
    name: model.match_context(match, history, table, cups=cups) for name, match in zip(names, fixtures.itertuples())
}

with info_tab:
    squad_box = st.container()  # a hiányzók és kezdők szekciója kerül legfelülre
    st.subheader("Időjárás, pihenőidő, rangadó, új edző")
    st.caption(
        "A pihenőnapokat és a rangadót a modell magától tölti ki a bajnoki és a nemzetközi kupameccsekből (BL, EL, KL); "
        "írd át, ha más miatt eltér. Kevés pihenő (legfeljebb 4 nap) több kapott és kevesebb rúgott gólt jelent, "
        "a bajnoki utáni 4 napon belüli kupameccs előtt pedig rotációval számol. Új edzőt az első 3 meccsén jelölj."
    )
    conditions = st.data_editor(
        pd.DataFrame(
            {
                "Meccs": names,
                "Időjárás": model.WEATHER["normal"][0],
                "Pihenő – hazai (nap)": [contexts[name]["rest"][0] for name in names],
                "Pihenő – vendég (nap)": [contexts[name]["rest"][1] for name in names],
                "Rangadó": [contexts[name]["derby"] for name in names],
                "Új edző – hazai": False,
                "Új edző – vendég": False,
            }
        ).astype({"Pihenő – hazai (nap)": "float", "Pihenő – vendég (nap)": "float"}),
        hide_index=True,
        disabled=["Meccs"],
        key=f"conditions-{league}-{season_start}-{selected_day}",
        column_config={
            "Időjárás": st.column_config.SelectboxColumn(options=list(WEATHER_KEYS), required=True),
            "Pihenő – hazai (nap)": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "Pihenő – vendég (nap)": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "Rangadó": st.column_config.CheckboxColumn(help="Rangadón több a lap, és a friss forma fele súllyal számít."),
            "Új edző – hazai": st.column_config.CheckboxColumn(help="Az új edző első 3 meccsén jelöld be."),
            "Új edző – vendég": st.column_config.CheckboxColumn(help="Az új edző első 3 meccsén jelöld be."),
        },
    )
    for row in conditions.to_dict("records"):
        contexts[row["Meccs"]].update(
            weather=WEATHER_KEYS.get(row["Időjárás"], "normal"),
            rest=(row["Pihenő – hazai (nap)"], row["Pihenő – vendég (nap)"]),
            derby=bool(row["Rangadó"]),
            new_coach=(bool(row["Új edző – hazai"]), bool(row["Új edző – vendég"])),
        )

    with squad_box:
        st.subheader(f"Sérülések, Eltiltások & Várható Kezdők – {model.LEAGUES[league]}")
        st.caption(
            "Adj hozzá sort minden sérülthöz és eltiltotthoz – a csapat támadó- és védekezőereje azonnal csökken "
            "a gólmodellben. Kulcsember (top góllövő, első kapus, kulcsvédő) hiánya 15–20%-kal számít, kulcsjátékosé "
            "néhány százalékkal, más játékosé ennek negyedével; kérdéses játékos fél súllyal."
        )
        blank = pd.DataFrame(
            {
                "Csapat": pd.Series(dtype="object"),
                "Játékos": pd.Series(dtype="object"),
                "Poszt": pd.Series(dtype="object"),
                "Ok": pd.Series(dtype="object"),
                "Kulcsjátékos": pd.Series(dtype="bool"),
                "Kulcsember": pd.Series(dtype="bool"),
                "Kérdéses": pd.Series(dtype="bool"),
            }
        )
        absences = st.data_editor(
            blank,
            num_rows="dynamic",
            hide_index=True,
            key=f"absences-{league}-{season_start}-{selected_day}",
            column_config={
                "Csapat": st.column_config.SelectboxColumn(options=teams, required=True),
                "Játékos": st.column_config.TextColumn(),
                "Poszt": st.column_config.SelectboxColumn(
                    options=list(POSITIONS), default="Támadó", required=True
                ),
                "Ok": st.column_config.SelectboxColumn(options=REASONS, default="sérült"),
                "Kulcsjátékos": st.column_config.CheckboxColumn(default=True),
                "Kulcsember": st.column_config.CheckboxColumn(
                    default=False,
                    help="Top góllövő, első számú kapus, kulcs-belsővédő vagy irányító: a hiánya 15–20%-kal számít.",
                ),
                "Kérdéses": st.column_config.CheckboxColumn(default=False),
            },
        )
        with st.expander("Hiányzók és kezdők betöltése fájlból (JSON)"):
            uploaded = st.file_uploader(
                "Csapathírek fájlja",
                type="json",
                key=f"team-news-{league}-{season_start}-{selected_day}",
                label_visibility="collapsed",
            )
            st.caption(
                "A team_news.example.json szerkezetét várja: meccsenként kezdők (players), hiányzók "
                "(missingPlayers: sérült, eltiltott, kérdéses) és kulcsjátékosok. Ide tölthető be a máshonnan "
                "(például Transfermarkt, FotMob, Premier Injuries) kézzel összeállított lista."
            )
        lineup_box = st.container()  # a számolás után töltjük ki
        news_box = st.container()

news = build_news(absences, fixtures)

if uploaded is not None:
    try:
        file_news = model.parse_team_news(json.load(uploaded))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        squad_box.error(f"Nem sikerült beolvasni a fájlt: {error!r}")
    else:
        for pair, entry in file_news.items():
            if pair not in news:
                continue
            news[pair]["referee"] = news[pair]["referee"] or entry["referee"]
            for side in ("home", "away"):
                add_absences(news, pair, side, entry[side])

# Kihirdetett kezdők: a hamarosan kezdődő és az élő meccseknél a kezdőből hiányzó szokásos kezdők
# automatikusan hiányzónak számítanak. Befejezett meccs tippjeit utólag már nem módosítja.
if not is_past:
    for match in fixtures.itertuples():
        pair = (match.home, match.away)
        score = scores.get(pair)
        if not score or score["state"] == "finished" or score["kickoff"] - pd.Timestamp.now(tz="UTC") > LINEUP_WINDOW:
            continue
        try:
            lineup = load_lineup(league, score["id"])
            for side in ("home", "away") if lineup else ():
                add_absences(news, pair, side, model.lineup_absences(lineup[side], load_usual(league, score["id"], side)))
        except (requests.RequestException, ValueError, KeyError):
            continue

# --- Számolás -----------------------------------------------------------------

stats, referees, details = [], [], {}
for name, match in zip(names, fixtures.itertuples()):
    analysis = model.analyse(match, models, history, news, corner_line, card_line, h2h, contexts[name])
    markets = analysis["markets"]
    details[name] = (match, analysis)

    stat_row = {"Meccs": name}
    for label, market, line in (("Szöglet", markets[3], corner_line), ("Sárga lap", markets[4], card_line)):
        home_value, away_value = market["values"]
        known = not math.isnan(home_value)
        stat_row[f"{label} – hazai"] = home_value if known else None
        stat_row[f"{label} – vendég"] = away_value if known else None
        stat_row[f"{label} – összesen"] = home_value + away_value if known else None
        over = market["outcomes"]["Több"] if market["outcomes"] else None
        stat_row[f"{label} {model.line_text(line)} felett"] = percent(over)
    if is_past:
        stat_row["Tényleges szöglet"] = match.home_corners + match.away_corners
        stat_row["Tényleges sárga"] = match.home_yellows + match.away_yellows
    stats.append(stat_row)

    referee = [note for note in analysis["notes"] if note.startswith("Játékvezető")]
    referees.append(
        {"Meccs": name, "Játékvezető és lapátlaga": referee[0].replace("Játékvezető: ", "") if referee else "nincs adat"}
    )

stats = pd.DataFrame(stats)
stat_columns = {
    column: st.column_config.ProgressColumn(column, format="%d%%", min_value=0, max_value=100)
    if column.endswith("felett")
    else st.column_config.NumberColumn(column, format="%.2f")
    for column in stats.columns
    if column.endswith("felett") or " – " in column and column != "Meccs"
}

# --- Szorzók: munkamenet + napló ----------------------------------------------

odds_scope = f"{ODDS_PREFIX}{league}|{season_start}|{bookmaker}"
odds_log = load_odds_log()


def odds_key(match_name: str, row: dict) -> str:
    return f"{odds_scope}|{match_name}|{row['market']}|{row['outcome']}"


def logged(key: str) -> list:
    return odds_log.get(key, [])


def confirmed_odds(value):
    """A szorzó, ha valódi, értelmes szám; minden más (üres, szöveg, NaN, 1,01 alatti érték) None.

    Szorzó csak innen kerül kiírásra: nincs alapértelmezett vagy becsült érték,
    és másik iroda szorzója sem helyettesíti a hiányzót.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 1.01 else None


def current_odds(key: str):
    """A kiválasztott irodához megadott szorzó, vagy None, ha nincs megerősített érték.

    Elsőbbsége az API-ból megérkezett szorzónak van; enélkül a mezőbe írt érték számít, ha pedig a
    mező ebben a munkamenetben még nem élt, a naplóból az utolsó (így a kezdés előtti utolsó szorzó megmarad).
    """
    if key in feed:
        return feed[key]
    if key in st.session_state:
        return confirmed_odds(st.session_state[key])
    return confirmed_odds(logged(key)[-1][1]) if logged(key) else None


def priced(match_name: str, row: dict) -> dict:
    """A kimenetel a megadott szorzóval, a value-val (modell − implikált esély) és a szorzó mozgásával."""
    key = odds_key(match_name, row)
    odds = current_odds(key)
    edge = row["p"] - model.implied_probability(odds) if odds else None
    return {**row, "odds": odds, "edge": edge, "movement": model.odds_movement(logged(key))}


# --- Automatikus szorzók (The Odds API) ---------------------------------------

feed = {}  # mezőkulcs -> az API-ból megérkezett szorzó; ami itt nincs, ahhoz nem jött adat
odds_status = ""
feed_bookmaker = model.ODDS_API_BOOKMAKERS.get(bookmaker)
if odds_api_key and feed_bookmaker and not is_past:
    try:
        odds_events, quota, odds_time = load_odds(league, feed_bookmaker, odds_api_key)
    except (model.OddsApiError, requests.RequestException, ValueError, KeyError):
        pass  # kapcsolat vagy adat nélkül a szorzók helye üresen marad, hibaüzenet nélkül
    else:
        api_prices = model.match_odds(fixtures, odds_events)
        for name, (match, analysis) in details.items():
            prices = api_prices.get((match.home, match.away), {})
            for row in model.selections(analysis):
                price = confirmed_odds(prices.get((row["market"], row["outcome"])))
                if price is not None:
                    feed[odds_key(name, row)] = price
        odds_status = (
            f"Szorzók: Unibet (The Odds API), frissítve {odds_time:%H:%M} · {len(api_prices)} meccshez érkezett adat"
            + (f" · a havi keretből {quota} kérés maradt" if quota else "")
            + ". Negyedóránként frissül. Csak az 1X2 és a megérkezett gólvonalak töltődnek be; a többi mező kézzel adható meg."
            + (" A Tippmixpro oszlopban az Unibet szorzói állnak." if bookmaker == "Tippmixpro" else "")
        )


def top_picks(match_name: str, analysis: dict, count: int = 3) -> list:
    """A meccs legvalószínűbb tippjei, piaccsoportonként legfeljebb egy.

    Kizárólag a modell esélye dönt (a csapatok statisztikái, formája, xG-je és a
    meccs körülményei alapján); a szorzó csak kiegészítő adatként kerül a tipp mellé.
    """
    best = {}
    for row in model.selections(analysis):
        if row["group"] not in best or row["p"] > best[row["group"]]["p"]:
            best[row["group"]] = row
    picks = sorted(best.values(), key=lambda row: row["p"], reverse=True)[:count]
    return [priced(match_name, row) for row in picks]


def pick_cells(pick: dict, won=None) -> str:
    """Egy tipp sora a meccskártyán: név, szorzó, modell-esély; befejezett meccsnél pipa vagy X."""
    top = pick["p"] > model.RECOMMENDED
    mark = "" if won is None else '<span class="mark win">✓</span>' if won else '<span class="mark lose">✗</span>'
    tags = '<span class="tag risk">MAGAS KOCKÁZAT</span>' if pick.get("risky") else ""
    # Megerősített szorzó nélkül a szorzó helye üresen marad.
    odds = f'<div class="cell">{pick["odds"]:.2f}</div>' if pick["odds"] else '<div class="cell blank"></div>'
    return (
        f'<div class="pick-name">{mark}{escape(pick["outcome"])}<small>{escape(pick["market"])}</small>{tags}</div>'
        f'{odds}<div class="cell prob{" top" if top else ""}">{pick["p"]:.0%}</div>'
    )


# --- 1. fül: ajánlott tippek --------------------------------------------------

with tips_tab:
    if odds_status:
        st.caption(odds_status)
    with st.expander(f"{bookmaker} szorzók megadása és teljes összevetés"):
        odds_match = st.selectbox("Meccs", list(details), key="odds-match")
        analysis = details[odds_match][1]
        rows = model.selections(analysis)
        expected_goals = sum(analysis["markets"][0]["values"])
        examined = ", ".join(
            f"{model.line_text(line)} {'felett' if side == 'Több' else 'alatt'}"
            for line, side in model.goal_lines(expected_goals)
        )
        st.caption(f"Várható gólszám: {expected_goals:.2f} → vizsgált gólvonalak: {examined}.")

        result_rows = [row for row in rows if row["group"] == "1X2"]
        other_rows = [row for row in rows if row["group"] != "1X2"]
        blocks = [result_rows] + [other_rows[start : start + 4] for start in range(0, len(other_rows), 4)]
        for block in blocks:
            for column, row in zip(st.columns(4), block):
                key = odds_key(odds_match, row)
                # Az API-ból érkezett szorzó felülírja a mezőt, és nem szerkeszthető; a többi kézzel adható meg.
                if key in feed or key not in st.session_state:
                    st.session_state[key] = current_odds(key)
                column.number_input(
                    f"{row['market']} – {row['outcome']}",
                    min_value=1.01,
                    step=0.01,
                    format="%.2f",
                    key=key,
                    disabled=key in feed,
                )

        # Minden változást naplóz, így a később átírt szorzóból látszik a piaci mozgás.
        now, changed = time.time(), False
        latest = {**{odds_key(odds_match, row): st.session_state.get(odds_key(odds_match, row)) for row in rows}, **feed}
        for key, value in latest.items():
            entries = model.record_odds(logged(key), value, now)
            if entries != logged(key):
                odds_log[key], changed = entries, True
        if changed:
            ODDS_LOG.write_text(json.dumps(odds_log, ensure_ascii=False), encoding="utf-8")

        comparison = pd.DataFrame(
            [
                {
                    "Piac": pick["market"],
                    "Kimenetel": pick["outcome"],
                    "Modell %": f"{pick['p'] * 100:.1f}",
                    "Fair szorzó": f"{1 / pick['p']:.2f}" if pick["p"] > 0 else "–",
                    f"{bookmaker} szorzó": f"{pick['odds']:.2f}" if pick["odds"] else "",
                    "Implikált %": f"{model.implied_probability(pick['odds']) * 100:.1f}" if pick["odds"] else "",
                    "Value (%p)": f"{pick['edge'] * 100:+.1f}" if pick["edge"] is not None else "",
                }
                for pick in (priced(odds_match, row) for row in rows)
            ]
        )
        st.dataframe(comparison, hide_index=True, height=35 * (len(comparison) + 1) + 3)
        st.caption("Implikált % = 1 / szorzó × 100 (az iroda árrésével együtt). Value = modell − implikált.")

    def pair(values) -> str:
        return "–" if not values or any(pd.isna(value) for value in values) else f"{values[0]:.0f}–{values[1]:.0f}"

    sections = {"live": [], "upcoming": [], "finished": []}
    settled = []  # a befejezett meccsek 70% feletti tippjei: nyert-e
    for name, (match, analysis) in details.items():
        score = scores.get((match.home, match.away), {"state": "upcoming"})
        state = score["state"]
        # Több mint RISK_ABSENCES hiányzó alapember bármelyik csapatnál: a meccs tippjei magas kockázatúak.
        risky = max(analysis["key_absences"].values()) > model.RISK_ABSENCES
        picks = [{**pick, "risky": risky} for pick in top_picks(name, analysis)]
        results = [model.outcome_won(pick, score) if state == "finished" else None for pick in picks]
        settled += [won for pick, won in zip(picks, results) if won is not None and pick["p"] > model.RECOMMENDED]

        badge, line, middle = "", "", '<span class="vs">–</span>'
        if state != "upcoming":
            middle = f'<span class="result">{pair(score["goals"])}</span>'
            line = f'Szöglet {pair(score["corners"])} · Sárga lap {pair(score["yellows"])}'
        if state == "live":
            left = model.remaining_minutes(score)
            clock = "szünet" if score.get("half_time") else score["clock"]
            badge = f'<span class="status live">● LIVE {escape(clock)}</span>'
            line += f" · kb. {left} perc van hátra" if left else " · hosszabbítás"
        elif state == "finished":
            badge = '<span class="status done">VÉGE</span>'
        elif score.get("note"):
            badge = f'<span class="status off">{escape(STATUS_NOTES.get(score["note"], score["note"]))}</span>'
        sections[state].append(
            f'<div class="match-card{" live" if state == "live" else ""}"><div class="match-meta">'
            f"<span>{kickoff_text(match.kickoff)}</span>{badge}</div>"
            f'<div class="match-teams">{escape(match.home)}{middle}{escape(match.away)}</div>'
            + (f'<div class="live-line">{line}</div>' if line else "")
            + f'<div class="picks"><div class="head">Tipp</div><div class="head">{escape(bookmaker)}</div>'
            f'<div class="head">Modell</div>'
            f'{"".join(pick_cells(pick, won) for pick, won in zip(picks, results))}</div></div>'
        )

    if settled:
        st.caption(
            f"A befejezett meccsek {model.RECOMMENDED:.0%} feletti tippjei közül {sum(settled)} nyert, "
            f"{len(settled) - sum(settled)} vesztett."
        )
    for state, title in (("live", "🔴 Élő mérkőzések"), ("upcoming", "Következő mérkőzések"), ("finished", "Befejezett mérkőzések")):
        if sections[state]:
            st.html(
                f'<div class="section">{title} ({len(sections[state])})</div>'
                f'<div class="match-grid">{"".join(sections[state])}</div>'
            )
    st.caption(
        f"Meccsenként a 3 legvalószínűbb tipp, piaconként egy – kizárólag a modell esélye alapján (statisztikák, "
        f"forma, xG, hiányzók, körülmények). Zöld a {model.RECOMMENDED:.0%} feletti esély. A {bookmaker} oszlop csak "
        f"tájékoztató szorzó, a tippek kiválasztásába nem számít bele. Magas kockázat: valamelyik csapatnál több mint "
        f"{model.RISK_ABSENCES} alapember hiányzik. "
        f"Befejezett meccsnél ✓ = a tipp nyert, ✗ = vesztett."
    )

# --- 2. fül: statisztikák és várható mutatók ----------------------------------


def split(home_value, away_value) -> str:
    return "–" if pd.isna(home_value) or pd.isna(away_value) else f"{home_value:.2f} – {away_value:.2f}"


def count_pair(home_value, away_value) -> str:
    return "–" if pd.isna(home_value) or pd.isna(away_value) else f"{home_value:.0f}–{away_value:.0f}"


def h2h_average(meetings: pd.DataFrame, team: str, stat: str):
    """A csapat átlaga az egymás elleni meccseken, akár otthon, akár idegenben játszott."""
    at_home = meetings["home"] == team
    return pd.concat([meetings.loc[at_home, f"home_{stat}"], meetings.loc[~at_home, f"away_{stat}"]]).mean()


with stats_tab:
    match, analysis = details[st.selectbox("Meccs", list(details), key="stats-match")]
    markets = analysis["markets"]
    meetings = model.h2h_meetings(h2h, match.home, match.away).tail(model.H2H_MATCHES)

    st.subheader("Várható mutatók")
    expected_rows = []
    for label, values, stat in (
        ("Gól", markets[0]["values"], "goals"),
        ("Szöglet", markets[3]["values"], "corners"),
        ("Sárga lap", markets[4]["values"], "yellows"),
    ):
        known = not math.isnan(values[0])
        expected_rows.append(
            {
                "Mutató": label,
                f"Várható – {match.home}": f"{values[0]:.2f}" if known else "–",
                f"Várható – {match.away}": f"{values[1]:.2f}" if known else "–",
                "Várható összesen": f"{sum(values):.2f}" if known else "–",
                "H2H átlag (hazai – vendég)": split(
                    h2h_average(meetings, match.home, stat), h2h_average(meetings, match.away, stat)
                ),
            }
        )
    st.dataframe(pd.DataFrame(expected_rows), hide_index=True)
    st.caption(
        f"A várható értékek már minden tényezőt tartalmaznak. H2H átlag: az utolsó {len(meetings)} egymás elleni "
        f"meccs csapatonkénti átlaga."
    )

    st.subheader("Csapatmutatók")
    team_rows = []
    for team in (match.home, match.away):
        row = {"Csapat": team, "Forma": form.get(team, "–")}
        for stat, label in (
            ("goals", "Gól: rúgott / kapott"),
            ("xg", "xG / xGA (lövésekből)"),
            ("corners", "Szöglet: saját / ellenfél"),
            ("yellows", "Sárga lap: saját / ellenfél"),
        ):
            team_model = models[stat]
            if team_model is None or team not in team_model["teams"].index:
                row[label] = "–"
                continue
            averages = team_model["teams"].loc[team]
            row[label] = f"{averages['own_avg']:.2f} / {averages['opponent_avg']:.2f}"
        team_rows.append(row)
    st.dataframe(pd.DataFrame(team_rows), hide_index=True)
    st.caption("Meccsenkénti átlagok. Forma: az utolsó meccsek a régebbitől az újabbig.")

    st.subheader("Egymás elleni meccsek")
    if meetings.empty:
        st.caption("Az elmúlt szezonokban nem játszottak egymással ebben a bajnokságban.")
    else:
        st.dataframe(
            pd.DataFrame(
                {
                    "Dátum": meetings["date"].dt.strftime("%Y.%m.%d."),
                    "Hazai": meetings["home"],
                    "Vendég": meetings["away"],
                    "Eredmény": [f"{row.home_goals}–{row.away_goals}" for row in meetings.itertuples()],
                    "Szöglet": [count_pair(row.home_corners, row.away_corners) for row in meetings.itertuples()],
                    "Sárga lap": [count_pair(row.home_yellows, row.away_yellows) for row in meetings.itertuples()],
                }
            ).iloc[::-1],
            hide_index=True,
        )

    st.subheader("Piacok esélyei")
    market_rows = []
    for market in markets:
        outcomes = market["outcomes"]
        if outcomes is None:
            market_rows.append({"Piac": market["name"], "Esélyek": "nincs adat"})
            continue
        tip, p = model.best(outcomes)
        market_rows.append(
            {
                "Piac": market["name"],
                "Esélyek": "  ·  ".join(f"{label} {chance:.0%}" for label, chance in outcomes.items()),
                "Tipp": tip,
                "Fair szorzó": f"{1 / p:.2f}",
            }
        )
    st.dataframe(pd.DataFrame(market_rows), hide_index=True)

    with st.expander("Várható szögletek és lapok – a nap összes meccse"):
        st.dataframe(stats, hide_index=True, column_config=stat_columns)
        st.caption("Várható értékek csapatonként és összesen; a sáv a határ feletti kimenetel esélye.")

# --- 3. fül, folytatás: kezdőcsapatok, hírek ------------------------------------


def chances(market: dict) -> str:
    return " · ".join(f"{label} {chance:.0%}" for label, chance in market["outcomes"].items())


with lineup_box:
    st.markdown("##### Kezdőcsapatok és a hiányzók hatása")
    lineup_name = st.selectbox("Meccs", list(details), key="lineup-match")
    match, analysis = details[lineup_name]
    pair = (match.home, match.away)

    missing_notes = [note for note in analysis["notes"] if note.startswith("Hiányzók")]
    if missing_notes:
        without = model.analyse(match, models, history, {}, corner_line, card_line, h2h, contexts[lineup_name])
        before, after = without["markets"][0], analysis["markets"][0]
        st.markdown(
            "\n".join(f"- {note}" for note in missing_notes)
            + f"\n- **Hatás a meccsre:** várható gól {before['values'][0]:.2f} – {before['values'][1]:.2f} → "
            f"**{after['values'][0]:.2f} – {after['values'][1]:.2f}**; 1X2 esélyek: {chances(before)} → "
            f"**{chances(after)}**"
        )
    else:
        st.caption("Ehhez a meccshez nincs megadott vagy a kezdőcsapatból észlelt hiányzó.")

    score = scores.get(pair, {})
    lineup = None
    if score.get("id"):
        try:
            lineup = load_lineup(league, score["id"])
        except (requests.RequestException, ValueError, KeyError):
            st.caption("A kezdőcsapatok most nem tölthetők le.")
    if lineup:
        for column, side, team in zip(st.columns(2), ("home", "away"), pair):
            squad = lineup[side]
            lines = [f"**{team}** · {squad['formation'] or 'felállás nem ismert'}"]
            for code, label in POSITION_NAMES.items():
                players = [player["name"] for player in squad["starters"] if player["position"] == code]
                if players:
                    lines.append(f"{label}: {', '.join(players)}")
            try:
                absent = model.lineup_absences(squad, load_usual(league, score["id"], side))
            except (requests.RequestException, ValueError, KeyError):
                absent = []
            if absent:
                lines.append(
                    "Szokásos kezdők közül nem kezd: "
                    + ", ".join(f"{absence['name']} ({absence['reason']})" for absence in absent)
                )
            column.markdown("  \n".join(lines))
        st.caption(
            f"Kihirdetett kezdőcsapatok (ESPN). Szokásos kezdő, aki a csapat utolsó meccsei közül legalább "
            f"{model.USUAL_STARTS} alkalommal kezdett. A kezdés előtti két órában és élő meccsnél az ilyen hiányzót a modell "
            f"magától beszámítja; befejezett meccs tippjeit utólag már nem módosítja."
        )
    elif is_past:
        st.caption("Lejátszott napnál a kezdőcsapatokat nem tölti le.")
    else:
        st.caption(
            "A kezdőcsapat még nincs kihirdetve – általában a kezdés előtt kb. egy órával jelenik meg. Előre jelzett "
            "(várható) kezdőcsapathoz nincs szabadon elérhető forrás; a várható hiányzókat a fenti táblázatban add meg."
        )

with news_box:
    st.markdown(f"##### Friss hírek sérülésekről, eltiltásokról és kezdőkről – {model.LEAGUES[league]}")
    sources = model.NEWS_SOURCES.get(league, [])
    try:
        articles, failed = load_news(league)
    except requests.RequestException:
        articles, failed = [], [source for source, url in sources if url]
    tagged = model.tag_news(articles, teams)
    relevant = [item for item in tagged if set(item["categories"]) & set(model.SQUAD_NEWS)][:25]
    if relevant:
        st.dataframe(
            pd.DataFrame(
                {
                    "Idő": [f"{item['published']:%m.%d. %H:%M}" if item["published"] is not None else "" for item in relevant],
                    "Forrás": [item["source"] for item in relevant],
                    "Téma": [
                        ", ".join(category for category in item["categories"] if category in model.SQUAD_NEWS)
                        for item in relevant
                    ],
                    "Csapat": [", ".join(item["teams"]) for item in relevant],
                    "Cím": [item["title"] for item in relevant],
                    "Cikk": [item["link"] for item in relevant],
                }
            ),
            hide_index=True,
            column_config={"Cikk": st.column_config.LinkColumn(display_text="megnyitás", width="small")},
        )
    else:
        st.caption("A nap csapatairól most nincs sérülésről, eltiltásról vagy kezdőcsapatról szóló friss cikk.")
    manual = [source for source, url in sources if not url]
    st.caption(
        f"Források: {', '.join(source for source, url in sources if url)}."
        + (f" Nincs géppel olvasható hírfolyama, kézzel követendő: {', '.join(manual)}." if manual else "")
        + (f" Most nem elérhető: {', '.join(failed)}." if failed else "")
        + " A cikkeket kulcsszavak alapján válogatja; a modell csak a fent megadott vagy a kezdőcsapatból "
        "észlelt hiányzókkal számol, a hírek tájékoztató jellegűek."
    )

# --- 3. fül, folytatás: játékvezetők, szorzómozgás, háttértényezők -------------

with info_tab:
    st.subheader("Játékvezetők")
    st.dataframe(pd.DataFrame(referees), hide_index=True)
    st.caption("A bíró lapátlaga a ligaátlaghoz mérve módosítja a várható sárga lapokat.")

    st.subheader("Szorzók mozgása (Market Drop)")
    moves = []
    for name, (_, analysis) in details.items():
        for pick in (priced(name, row) for row in model.selections(analysis)):
            movement = pick["movement"]
            if not movement:
                continue
            top = pick["p"] > model.RECOMMENDED
            if movement["drop"] and top:
                st.success(
                    f"📉 **{name}** – {pick['market']} · {pick['outcome']}: a szorzó esett "
                    f"({movement_text(movement)}), és ez egybeesik a {pick['p']:.0%} esélyű tippünkkel."
                )
            moves.append(
                {
                    "Meccs": name,
                    "Piac": pick["market"],
                    "Kimenetel": pick["outcome"],
                    "Nyitó → mostani": movement_text(movement),
                    "Drop": "igen" if movement["drop"] else "–",
                    "Modell %": f"{pick['p'] * 100:.0f}",
                    f"{model.RECOMMENDED:.0%}+ tipp": "igen" if top else "–",
                }
            )
    if moves:
        st.dataframe(pd.DataFrame(moves), hide_index=True)
    st.caption(
        f"A mozgást a beírt {bookmaker} szorzókból követi: az elsőként megadott értéket veti össze a mostanival. "
        f"Legalább {model.DROP_THRESHOLD:.0%} esés számít dropnak."
        + ("" if moves else " Ezen a napon még nincs átírt szorzó.")
    )

    with st.expander("Aktív háttértényezők meccsenként"):
        for name, (_, analysis) in details.items():
            if analysis["notes"]:
                st.markdown(f"**{name}**\n\n" + "\n".join(f"- {note}" for note in analysis["notes"]))

# --- 4. fül: hírek és friss fejlemények ----------------------------------------

with news_tab:
    source_names = ", ".join(source for source, url in sources if url)
    st.caption(
        f"{model.LEAGUES[league]} – források: {source_names}. A cikkeket kulcsszavak alapján válogatja és sorolja "
        "témába; a „Várható hatás” a téma alapján készül, nem a cikk szövegének értelmezéséből."
        + (" Lejátszott napnál is a mostani híreket mutatja." if is_past else "")
    )
    view = st.radio("Nézet", ["Meccsenként", "A liga összes friss híre"], horizontal=True, label_visibility="collapsed")

    if view == "Meccsenként":
        shown = 0
        for name, (match, analysis) in details.items():
            items = [item for item in tagged if {match.home, match.away} & set(item["teams"])][:6]
            if not items:
                continue
            shown += 1
            with st.container(border=True):
                st.markdown(f"#### {name}")
                st.markdown("\n".join(crumb(item) for item in items))

                categories = [category for category in NEWS_IMPACT if any(category in item["categories"] for item in items)]
                impact = [
                    f"- {NEWS_IMPACT[category][0]} **{category.capitalize()}:** {NEWS_IMPACT[category][1]}"
                    for category in categories
                ]
                result = analysis["markets"][0]
                state = f"- **A modell most:** {chances(result)}; várható gól {result['values'][0]:.2f} – {result['values'][1]:.2f}."
                final = scores.get((match.home, match.away), {})
                if final.get("state") == "finished":
                    state = (
                        f"- **A meccs már véget ért** ({final['goals'][0]}–{final['goals'][1]}); a modell előtte ezt adta: "
                        f"{chances(result)}. A fenti hírek a következő meccsekre nézve érdekesek."
                    )
                elif any(note.startswith("Hiányzók") for note in analysis["notes"]):
                    without = model.analyse(match, models, history, {}, corner_line, card_line, h2h, contexts[name])
                    before = without["markets"][0]
                    state += (
                        f" A már megadott hiányzók nélkül {chances(before)}, várható gól "
                        f"{before['values'][0]:.2f} – {before['values'][1]:.2f} lenne."
                    )
                else:
                    state += " Ehhez a meccshez még nincs megadott hiányzó, így a fenti hírek nincsenek beszámítva."
                moves = [
                    f"{pick['market']} {pick['outcome']}: {movement_text(pick['movement'])}"
                    for pick in (priced(name, row) for row in model.selections(analysis))
                    if pick["movement"]
                ]
                if moves:
                    state += f" Szorzómozgás ({bookmaker}): {'; '.join(moves)}."
                st.markdown("**Várható hatás a meccsre és a szorzókra**\n\n" + "\n".join([*impact, state]))
        if not shown:
            st.info("A nap meccseiről most nincs olyan friss hír, amely a kimenetelt érdemben érintené.")
    else:
        if articles:
            marks = {item["link"]: item for item in tagged}
            lines = []
            for item in articles[:40]:
                found = marks.get(item["link"])
                lines.append(crumb({**item, "categories": found["categories"] if found else []}))
            st.markdown("\n".join(lines))
        else:
            st.info("A hírforrások most nem érhetők el.")
    if failed:
        st.caption(f"Most nem elérhető forrás: {', '.join(failed)}.")

st.caption(
    f"Statisztikai becslés, nem garancia. Az esélyekben az utolsó {model.H2H_MATCHES} egymás elleni meccs "
    f"legfeljebb {model.H2H_WEIGHT:.0%} súllyal számít; a gólokba a lövésekből becsült xG, a tabellahelyzet, "
    f"a pihenőnapok és az időjárás, a lapokba a rangadó és az időjárás is beleszámít. "
    f"A hiányzók és a többi tényező hatása becsült szorzókkal számol (lásd a main.py elejét)."
)

# A Streamlit angol nyelvűnek jelöli az oldalt, ezért a böngésző gépi fordítója "lefordíthatja" és eltorzíthatja
# a magyar feliratokat. Az oldalt magyarnak jelöljük, és a gépi fordítást kikapcsoljuk.
components.html(
    """<script>
    const page = window.parent.document;
    page.documentElement.lang = "hu";
    page.documentElement.setAttribute("translate", "no");
    page.body.classList.add("notranslate");
    if (!page.querySelector('meta[name="google"]')) {
        const meta = page.createElement("meta");
        meta.name = "google";
        meta.content = "notranslate";
        page.head.appendChild(meta);
    }
    </script>""",
    height=0,
)
