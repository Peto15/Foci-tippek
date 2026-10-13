"""Visszamérés: mennyire találtak volna a modell tippjei a már lejátszott meccseken.

Minden meccsnapra csak az addig lejátszott meccsekből építi fel a modellt (ahogy
élesben is), megtippeli az aznapi meccseket, majd összeveti a tényleges
eredménnyel és a fogadóirodák átlagszorzóiból számolt esélyekkel.

Amit nem lát: hiányzók, kezdőcsapatok, időjárás, nemzetközi kupameccsek és
edzőváltás – ezekről nincs múltbeli adat, ezért a visszamérés az alapmodellt
(átlagok, forma, xG, egymás elleni meccsek, pihenőnap, motiváció, rangadó,
játékvezető) minősíti.

Használat:
    python backtest.py                       # öt bajnokság, az utolsó öt lezárt szezon és az idei
    python backtest.py --league E0 --from 2023
    python backtest.py --set H2H_WEIGHT=0.1 --set FORM_WEIGHT=0.3   # más beállítás kipróbálása
    python backtest.py --save meres.csv      # a meccsenkénti sorok mentése

A letöltött szezonfájlok a .cache mappába kerülnek; a lezárt szezonokat nem tölti le újra.
"""

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import main as model

CACHE_DIR = Path(__file__).with_name(".cache")
TEST_SEASONS = 5  # ennyi lezárt szezont mér vissza az idei mellett
# A fogadóirodák átlagszorzói a forrásfájlban, elsőbbségi sorrendben (a régebbi szezonok más oszlopnevet használnak).
ODDS_1X2 = [("AvgH", "AvgD", "AvgA"), ("BbAvH", "BbAvD", "BbAvA"), ("B365H", "B365D", "B365A")]
ODDS_TOTAL = [("Avg>2.5", "Avg<2.5"), ("BbAv>2.5", "BbAv<2.5"), ("B365>2.5", "B365<2.5")]
BUCKETS = (0.5, 0.6, 0.7, 0.8, 1.01)  # a tippek esélysávjai a találati arányhoz
NAN = float("nan")


def load_raw(league: str, start_year: int) -> pd.DataFrame:
    """Egy szezon forrásfájlja; a lezárt szezonokat a .cache mappából veszi, ha már megvan."""
    path = CACHE_DIR / f"{league}_{model.season_code(start_year)}.csv"
    if path.exists() and start_year < model.current_season_start():
        return pd.read_csv(path)
    raw = model.download_csv(model.DATA_URL.format(season=model.season_code(start_year), league=league))
    CACHE_DIR.mkdir(exist_ok=True)
    # Ideiglenes fájlból nevezi át, hogy a párhuzamosan futó mérések ne olvassanak félkész fájlt.
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    raw.to_csv(temporary, index=False)
    temporary.replace(path)
    return raw


def load_possession(league: str, start_year: int, teams: list) -> pd.DataFrame:
    """A szezon meccseinek labdabirtoklása; a lezárt szezonokat a .cache mappából veszi, ha már megvan."""
    path = CACHE_DIR / f"possession_{league}_{model.season_code(start_year)}.csv"
    if path.exists() and start_year < model.current_season_start():
        return pd.read_csv(path)
    possession = model.fetch_possession(league, start_year, teams)
    CACHE_DIR.mkdir(exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    possession.to_csv(temporary, index=False)
    temporary.replace(path)
    return possession


def load_season(league: str, start_year: int) -> pd.DataFrame:
    """Egy szezon lejátszott meccsei a modell oszlopaival, labdabirtoklással és a piac esélyeivel (market_1 / X / 2 / over)."""
    raw = load_raw(league, start_year)
    matches = model.parse_matches(raw)
    odds = raw.loc[matches.index]

    def implied(groups: list) -> pd.DataFrame:
        """Az első meglévő oszlopcsoport szorzóiból az árrés nélküli esélyek."""
        result = pd.DataFrame(NAN, index=matches.index, columns=range(len(groups[0])))
        for columns in groups:
            if not set(columns) <= set(odds.columns):
                continue
            inverse = 1 / odds[list(columns)].apply(pd.to_numeric, errors="coerce")
            inverse.columns = result.columns
            result = result.fillna(inverse.div(inverse.sum(axis=1, skipna=False), axis=0))
        return result

    matches[["market_1", "market_X", "market_2"]] = implied(ODDS_1X2)
    matches[["market_over", "market_under"]] = implied(ODDS_TOTAL)
    teams = sorted(set(matches["home"]) | set(matches["away"]))
    matches = model.add_possession(matches, load_possession(league, start_year, teams))
    return matches.sort_values("date", kind="stable").reset_index(drop=True)



def backtest_season(task: tuple) -> list:
    """Egy bajnokság egy szezonjának visszamérése: meccsenként egy sor a modell esélyeivel és a tényekkel."""
    league, start_year, overrides = task
    for name, value in overrides.items():
        setattr(model, name, value)
    try:
        current = load_season(league, start_year)
        previous = load_season(league, start_year - 1)
    except (requests.RequestException, KeyError, ValueError):
        return []
    older = model.older_seasons(league, start_year, load_season)

    rows = []
    for day, todays in current.groupby("date", sort=True):
        played = current[current["date"] < day]
        history = pd.concat([previous.assign(weight=model.PREV_SEASON_WEIGHT), played.assign(weight=1.0)], ignore_index=True)
        # A rangadós (kisebb formasúlyú) modellek csak rangadós napon kellenek; így a mérés közel kétszer gyorsabb.
        with_xg = model.add_xg(history)
        models = {stat: model.stat_model(with_xg, stat) for stat in (*model.STATS, "xg")}
        models["styles"] = model.team_styles(history)
        if any(model.is_derby(home, away) for home, away in zip(todays["home"], todays["away"])):
            models["derby"] = model.build_models(history)["derby"]
        table = model.build_table(played) if not played.empty else None
        h2h = pd.concat([*older, previous, played], ignore_index=True)
        games = pd.concat([played["home"], played["away"]]).value_counts()
        for match in todays.assign(kickoff=day).itertuples():
            context = model.match_context(match, history, table)
            analysis = model.analyse(match, models, history, {}, model.CORNER_LINE, model.CARD_LINE, h2h, context)
            markets = analysis["markets"]
            home_goals, away_goals = markets[0]["values"]
            row = {
                "league": league,
                "season": start_year,
                "date": day,
                "home": match.home,
                "away": match.away,
                # A kevesebbet játszott csapat idei meccseinek száma: a szezon eleji tippek különválasztásához.
                "games": int(min(games.get(match.home, 0), games.get(match.away, 0))),
                "new_team": not {match.home, match.away} <= set(models["goals"]["teams"].index),
                "exp_home_goals": home_goals,
                "exp_away_goals": away_goals,
                "exp_corners": sum(markets[3]["values"]),
                "exp_yellows": sum(markets[4]["values"]),
                # A ligaátlag felé húzás és a stíluskorrekció előtti értékek, valamint a stíluspár: ezekből illeszthetők az állandók.
                "raw_goals": sum(analysis["raw"]["goals"]),
                "raw_corners": sum(analysis["raw"]["corners"]),
                "raw_yellows": sum(analysis["raw"]["yellows"]),
                "league_goals": models["goals"]["home_avg"] + models["goals"]["away_avg"],
                "league_corners": models["corners"]["home_avg"] + models["corners"]["away_avg"] if models["corners"] else NAN,
                "league_yellows": models["yellows"]["home_avg"] + models["yellows"]["away_avg"] if models["yellows"] else NAN,
                "style": "-".join(analysis["style"]["key"]) if analysis["style"] else "",
                "home_goals": match.home_goals,
                "away_goals": match.away_goals,
                "corners": match.home_corners + match.away_corners,
                "yellows": match.home_yellows + match.away_yellows,
                "market_1": match.market_1,
                "market_X": match.market_X,
                "market_2": match.market_2,
                "market_over": match.market_over,
            }
            for selection in model.selections(analysis):
                row[f"p|{selection['market']}|{selection['outcome']}"] = selection["p"]
            rows.append(row)
    return rows


def run(leagues: list, seasons: list, overrides: dict, workers: int = 4) -> pd.DataFrame:
    """A megadott bajnokságok és szezonok visszamérése párhuzamosan."""
    tasks = [(league, season, overrides) for league in leagues for season in seasons]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        rows = [row for result in pool.map(backtest_season, tasks) for row in result]
    return pd.DataFrame(rows)


def outcomes(results: pd.DataFrame) -> dict:
    """Piaconként a kimenetelek tényleges bekövetkezése: piac -> {kimenetel: igaz/hamis sor} (hiányzó adatnál NaN)."""
    home, away = results["home_goals"], results["away_goals"]
    total = home + away
    actual = {
        "1X2": {"1": home > away, "X": home == away, "2": home < away},
        "Mindkét csapat szerez gólt": {"Igen": (home > 0) & (away > 0), "Nem": (home == 0) | (away == 0)},
    }
    for line in model.GOAL_LINES:
        actual[f"Gólszám {model.line_text(line)}"] = {"Több": total > line, "Kevesebb": total < line}
    for name, column, line in (
        ("Szögletszám", "corners", model.CORNER_LINE),
        ("Sárga lapok", "yellows", model.CARD_LINE),
    ):
        known = results[column].notna()
        actual[f"{name} {model.line_text(line)}"] = {
            "Több": (results[column] > line).where(known),
            "Kevesebb": (results[column] < line).where(known),
        }
    return actual


def log_loss(probabilities: pd.DataFrame, happened: pd.DataFrame) -> float:
    """Átlagos logaritmikus veszteség: minél kisebb, annál jobbak az esélyek (a tényleges kimenetel esélyének -ln-je)."""
    chosen = (probabilities.clip(1e-9, 1) * happened).sum(axis=1)
    return float(-np.log(chosen).mean())


def summarise(results: pd.DataFrame) -> pd.DataFrame:
    """Piaconként a modell minősége: veszteség a piachoz és az alapgyakorisághoz képest, találati arány."""
    lines = []
    for market, sides in outcomes(results).items():
        happened = pd.DataFrame(sides)
        probabilities = pd.DataFrame({side: results.get(f"p|{market}|{side}", NAN) for side in sides})
        known = happened.notna().all(axis=1) & probabilities.notna().all(axis=1)
        happened, probabilities = happened[known].astype(float), probabilities[known]
        if happened.empty:
            continue
        frequency = pd.DataFrame(
            np.tile(happened.mean().to_numpy(), (len(happened), 1)), index=happened.index, columns=happened.columns
        )
        line = {
            "piac": market,
            "meccs": len(happened),
            "modell": log_loss(probabilities, happened),
            "alap": log_loss(frequency, happened),
            "piaci": NAN,
            "találat": float((probabilities.idxmax(axis=1) == happened.idxmax(axis=1)).mean()),
        }
        columns = {"1X2": ["market_1", "market_X", "market_2"], f"Gólszám {model.line_text(model.GOAL_LINE)}": ["market_over"]}
        if market in columns:
            priced = results.loc[known, columns[market]].copy()
            if market != "1X2":
                priced["market_under"] = 1 - priced["market_over"]
            priced.columns = happened.columns
            both = priced.notna().all(axis=1)
            line["piaci"] = log_loss(priced[both], happened[both])
            line["modell*"] = log_loss(probabilities[both], happened[both])
        lines.append(line)
    return pd.DataFrame(lines)


def calibration(results: pd.DataFrame) -> pd.DataFrame:
    """A tippek (piaconként a legesélyesebb kimenetel) találati aránya esélysávonként: ígért és tényleges."""
    picks = []
    for market, sides in outcomes(results).items():
        happened = pd.DataFrame(sides)
        probabilities = pd.DataFrame({side: results.get(f"p|{market}|{side}", NAN) for side in sides})
        known = happened.notna().all(axis=1) & probabilities.notna().all(axis=1)
        happened, probabilities = happened[known].astype(float), probabilities[known]
        tip = probabilities.idxmax(axis=1)
        picks.append(
            pd.DataFrame(
                {
                    "piac": market,
                    "p": probabilities.max(axis=1),
                    "nyert": [happened.at[index, side] for index, side in tip.items()],
                }
            )
        )
    picks = pd.concat(picks, ignore_index=True)
    picks["sáv"] = pd.cut(picks["p"], BUCKETS, right=False)
    table = picks.groupby(["piac", "sáv"], observed=True).agg(tipp=("p", "size"), ígért=("p", "mean"), tényleges=("nyert", "mean"))
    return table.reset_index()


def parse_overrides(items: list) -> dict:
    """A --set NÉV=érték párok a modell állandóinak felülírásához."""
    overrides = {}
    for item in items or []:
        name, _, value = item.partition("=")
        if not hasattr(model, name):
            raise SystemExit(f"Nincs ilyen beállítás a modellben: {name}")
        overrides[name] = type(getattr(model, name))(value)
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(description="A tippmodell visszamérése lejátszott meccseken")
    parser.add_argument("--league", action="append", choices=sorted(set(model.LEAGUES) - model.EUROPEAN_CUPS), help="bajnokság (többször is megadható)")
    parser.add_argument("--from", dest="first", type=int, help="az első visszamért szezon kezdőéve")
    parser.add_argument("--to", dest="last", type=int, help="az utolsó visszamért szezon kezdőéve")
    parser.add_argument("--set", action="append", metavar="NÉV=ÉRTÉK", help="a modell egy állandójának felülírása")
    parser.add_argument("--save", metavar="FÁJL", help="a meccsenkénti sorok mentése CSV-be")
    args = parser.parse_args()

    last = args.last or model.current_season_start()
    first = args.first or last - TEST_SEASONS
    overrides = parse_overrides(args.set)
    for name, value in overrides.items():  # a kiértékelés (például a határok) ugyanazzal a beállítással számoljon
        setattr(model, name, value)
    results = run(args.league or sorted(set(model.LEAGUES) - model.EUROPEAN_CUPS), list(range(first, last + 1)), overrides)
    if results.empty:
        print("Nincs visszamérhető meccs.", file=sys.stderr)
        return 1
    if args.save:
        results.to_csv(args.save, index=False)

    pd.set_option("display.width", 200)
    print(f"Visszamérés: {len(results)} meccs, {first}/{(first + 1) % 100:02d} – {last}/{(last + 1) % 100:02d}\n")
    print("Logaritmikus veszteség (kisebb = jobb). alap: mindig a kimenetel gyakoriságát mondja;")
    print("piaci: a fogadóirodák átlagszorzói árrés nélkül; modell*: a modell ugyanazokon a meccseken, ahol van szorzó.\n")
    print(summarise(results).to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print("\nA tippek találati aránya esélysávonként (ígért = a modell átlagos esélye, tényleges = ennyi jött be):\n")
    print(calibration(results).to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
