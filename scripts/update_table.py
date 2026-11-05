#!/usr/bin/env python3
"""
Roshambo Praha – automatická synchronizace webu se sipky.org (verze 2.0)

Při každém běhu:
  1. zjistí aktuální sezónu, ligu a ID družstva (není potřeba nic měnit na začátku sezóny),
  2. stáhne tabulku, rozpis s výsledky a hracími místy, statistiky hráčů celé ligy a soupisku,
  3. nová hrací místa doplní o adresu a GPS z jejich stránky na sipky.org,
  4. když začne nová sezóna, přesune tu skončenou do historie,
  5. data zapíše do bloku <script id="site-data"> v index.html – stránka se z nich vykreslí sama.

Každá část se aktualizuje nezávisle. Co se nepodaří stáhnout nebo nevypadá věrohodně, zůstane
v poslední platné podobě a do přehledu běhu se zapíše varování. Když se nepodaří stáhnout vůbec nic,
skript skončí chybou (běh na GitHubu zčervená), aby výpadek nezůstal bez povšimnutí.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- konfigurace
TEAM_NAME = "Roshambo Praha"
TEAM_ID = "339464"              # stálé ID družstva na sipky.org (mezi sezónami se nemění)
REGION = "stc"
LEAGUE_TYPE = "611"             # Hlavní ligová soutěž UŠO
BASE_URL = "https://www.sipky.org/"
INDEX_FILE = Path(os.environ.get("INDEX_FILE", "index.html"))
TZ = ZoneInfo("Europe/Prague")
FETCH_DEADLINE_S = 300          # strop na veškeré stahování v jednom běhu
MAX_NEW_VENUES_PER_RUN = 5

STAT_KEYS = ("lkh", "zp", "zc", "zo", "zv", "lp", "lo", "lv",
             "n95", "n95a", "n133", "n133a", "n170", "n170a", "z64", "z53", "z42", "z3")
INT_KEYS = {"zc", "zo", "zv", "lo", "lv", "n95", "n133", "n170", "z64", "z53", "z42", "z3"}

DATA_ISLAND = re.compile(r'(<script type="application/json" id="site-data">)(.*?)(</script>)', re.S)
CONFIG_ISLAND = re.compile(r'<script type="application/json" id="site-config">(.*?)</script>', re.S)
POSITION = re.compile(r"^\d+\.$")
DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")
GPS = re.compile(r"GPS:\s*([NS])\s*(\d+)°\s*([\d.]+)'\s*,?\s*([EW])\s*(\d+)°\s*([\d.]+)'")

log = logging.getLogger("roshambo-sync")


def page_url(**params: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"region": REGION, **params}.items())
    return f"{BASE_URL}?{query}"


# --------------------------------------------------------------------------- stahování
class Fetcher:
    """Stáhne stránku přímo, přes ScraperAPI nebo přes Jina Reader (v tomto pořadí).

    GitHub Actions mají na sipky.org zablokované IP adresy, takže v praxi rozhoduje ScraperAPI.
    Každá stránka se v rámci běhu stahuje nejvýš jednou a celé stahování má časový strop.
    """

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

    def __init__(self, scraper_key: str | None, deadline_s: int = FETCH_DEADLINE_S) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.UA, "Accept-Language": "cs,en;q=0.8"})
        self.scraper_key = scraper_key
        self.deadline = time.monotonic() + deadline_s
        self.cache: dict[str, str | None] = {}
        self.requests = 0

    def get(self, url: str) -> str | None:
        if url not in self.cache:
            self.cache[url] = self._fetch(url)
        return self.cache[url]

    def _fetch(self, url: str) -> str | None:
        attempts = [("přímo", url, {}, (10, 20))]
        if self.scraper_key:
            proxied = f"https://api.scraperapi.com/?api_key={self.scraper_key}&url={quote(url, safe='')}"
            attempts += [("ScraperAPI", proxied, {}, (10, 70))] * 2
        attempts.append(("Jina", f"https://r.jina.ai/{url}", {"X-Return-Format": "html"}, (10, 40)))

        for name, target, headers, timeout in attempts:
            if time.monotonic() > self.deadline:
                log.warning("Vypršel časový limit stahování, %s se přeskakuje.", url)
                return None
            try:
                self.requests += 1
                resp = self.session.get(target, headers=headers, timeout=timeout)
                if resp.ok:
                    html = decode(resp.content)
                    if is_plausible(html):
                        log.info("Staženo (%s): %s", name, url)
                        return html
                log.info("%s: HTTP %s pro %s", name, resp.status_code, url)
            except requests.RequestException as exc:
                log.info("%s: %s pro %s", name, type(exc).__name__, url)
        return None


def decode(content: bytes) -> str:
    """sipky.org posílá windows-1250, proxy služby už často UTF-8."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1250", errors="replace")


def is_plausible(html: str) -> bool:
    return len(html) > 1500 and "<table" in html.lower()


# --------------------------------------------------------------------------- parsery
def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _int(value: str) -> int | str:
    return int(value) if re.fullmatch(r"-?\d+", value.strip()) else value.strip()


def parse_season(html: str) -> dict:
    """ID a název sezóny, ligy a družstva z odkazů na stránce družstva."""
    soup = _soup(html)
    info: dict = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"team={TEAM_ID}" in href:
            if "leagueTeamId" not in info and (m := re.search(r"league_team=(\d+)", href)):
                info["leagueTeamId"] = m.group(1)
                if lm := re.search(r"[?&]league=(\d+)", href):
                    info["leagueId"] = lm.group(1)
            if "id" not in info and (m := re.search(r"season=(\d+)", href)):
                info["id"] = m.group(1)
    if league_id := info.get("leagueId"):
        link = soup.find("a", href=re.compile(rf"page=ligova-skupina.*league={league_id}\b"))
        if link and link.get_text(strip=True):
            info["league"] = link.get_text(strip=True)
    for option in soup.find_all("option", selected=True):       # vybraná položka v seznamu sezón
        if m := re.fullmatch(r"\s*(\d{4}/\d{4})\s*", option.get_text()):
            info["label"] = m.group(1)
            if "id" not in info and re.fullmatch(r"\d+", option.get("value", "")):
                info["id"] = option["value"]
            break
    return info


def parse_standings(html: str) -> tuple[list[dict], bool]:
    """Tabulka ligové skupiny. Sloupce na sipky.org:
    #, družstvo, kol, V, VP, R, PP, P, K, skóre (3 buňky), legy (3 buňky), body."""
    final = "konečné pořadí" in html.lower()
    rows = []
    for tr in _soup(html).find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 16 or not POSITION.match(cells[0]):
            continue
        link = tr.find_all("td")[1].find("a", href=True)
        lt = re.search(r"league_team=(\d+)", link["href"]) if link else None
        try:
            rows.append({
                "pos": int(cells[0].rstrip(".")), "name": cells[1], "lt": lt.group(1) if lt else "",
                "body": int(cells[15]), "kol": int(cells[2]), "v": int(cells[3]),
                "r": int(cells[5]), "p": int(cells[7]),
                "legy": f"{cells[12]}:{cells[14]}", "skore": f"{cells[9]}:{cells[11]}",
            })
        except ValueError:
            continue
        if len(rows) > 1 and rows[-1]["pos"] < rows[-2]["pos"]:   # začala další tabulka na stránce
            rows.pop()
            break
    return rows, final


def parse_schedule(html: str) -> list[dict]:
    """Rozpis zápasů družstva: kolo, datum, čas, týmy, skóre a hrací místo."""
    matches: dict[int, dict] = {}
    for venue in _soup(html).find_all("a", href=re.compile(r"page=hraci-misto")):
        tr = venue.find_parent("tr")
        round_link = tr.find("a", href=re.compile(r"round=\d+")) if tr else None
        if round_link is None:
            continue
        text = tr.get_text(" ", strip=True)
        day = DATE.search(text)
        cells = tr.find_all("td", recursive=False) or tr.find_all("td")
        team_idx = [i for i, td in enumerate(cells) if td.find("a", href=re.compile(r"page=profil-druzstva"))]
        if not day or len(team_idx) < 2:
            continue
        home, away = (cells[i].find("a", href=re.compile(r"page=profil-druzstva")).get_text(strip=True)
                      for i in team_idx[:2])
        between = " ".join(td.get_text(" ", strip=True) for td in cells[team_idx[0] + 1:team_idx[1]])
        score = SCORE.search(between)

        clock = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}\s+(\d{1,2}:\d{2})\b", text)
        if not clock and (nxt := tr.find_next_sibling("tr")):
            clock = re.match(r"\s*(\d{1,2}:\d{2})\b", nxt.get_text(" ", strip=True))

        rnd = int(re.search(r"round=(\d+)", round_link["href"]).group(1))
        place = re.search(r"place=(\d+)", venue["href"])
        d, m, y = (int(x) for x in day.groups())
        match = {
            "round": rnd, "date": date(y, m, d).isoformat(), "time": clock.group(1) if clock else "",
            "home": home, "away": away, "place": place.group(1) if place else "",
            "venue": venue.get_text(strip=True), "score": None, "result": None,
        }
        if score:
            ours, theirs = (int(x) for x in score.groups())
            if away == TEAM_NAME:
                ours, theirs = theirs, ours
            match["score"] = f"{score.group(1)}:{score.group(2)}"
            match["result"] = "win" if ours > theirs else "loss" if ours < theirs else "draw"
        matches[rnd] = match
    return [matches[k] for k in sorted(matches)]


def parse_players(html: str) -> list[dict]:
    """Statistika hráčů (družstva nebo celé ligy). U celé ligy přidá číslo družstva 'lt'."""
    players = []
    for tr in _soup(html).find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7 or not POSITION.match(tds[0].get_text(strip=True)):
            continue
        person = tds[1].find("a", href=re.compile(r"osobni-profil"))
        if person is None:
            continue                                   # řádek tabulky družstev, ne hráč
        team = tds[1].find("a", href=re.compile(r"league_team=\d+"))
        values = [td.get_text(strip=True) for td in tds[2:2 + len(STAT_KEYS)]]
        values += [""] * (len(STAT_KEYS) - len(values))
        player = {"name": person.get_text(strip=True)}
        player.update({k: (_int(v) if k in INT_KEYS else v) for k, v in zip(STAT_KEYS, values)})
        if team:
            player["lt"] = re.search(r"league_team=(\d+)", team["href"]).group(1)
        if isinstance(player["zo"], int) and isinstance(player["zv"], int) and player["zo"] > 0:
            players.append(player)
    return players


def parse_roster(html: str) -> tuple[list[str], str | None]:
    """Soupiska (tabulka se sloupcem 'Souhlas s GDPR') a vedoucí družstva."""
    soup = _soup(html)
    members: list[str] = []
    captain = None
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        text = table.get_text(" ", strip=True)
        if "Vedoucí družstva" in text and captain is None:
            flag = False
            for tr in table.find_all("tr"):
                if "Vedoucí družstva" in tr.get_text():
                    flag = True
                elif flag and (a := tr.find("a", href=re.compile(r"osobni-profil"))):
                    captain = a.get_text(strip=True)
                    break
        if "Souhlas s GDPR" in text and not members:
            for tr in table.find_all("tr"):
                a = tr.find("a", href=re.compile(r"osobni-profil"))
                if a and re.search(r"CZE\d+", tr.get_text()) and a.get_text(strip=True) not in members:
                    members.append(a.get_text(strip=True))
    return members, captain


def parse_venue(html: str) -> dict:
    """Adresa a GPS hracího místa (formát 'GPS: N 50° 5.486', E 14° 27.158'')."""
    soup = _soup(html)
    node = soup.find(string=GPS)                   # text "ulice, PSČ obec, GPS: ..." v jednom prvku
    text = str(node) if node else soup.get_text(" ", strip=True)
    venue: dict = {}
    if m := GPS.search(text):
        ns, lat_d, lat_m, ew, lng_d, lng_m = m.groups()
        venue["lat"] = round((int(lat_d) + float(lat_m) / 60) * (1 if ns == "N" else -1), 6)
        venue["lng"] = round((int(lng_d) + float(lng_m) / 60) * (1 if ew == "E" else -1), 6)
        before = " ".join(text[:m.start()].split()).rstrip(" ,")
        if a := re.search(r"([^,]{2,60}\d[^,]{0,20},\s*\d{3}\s?\d{2}\s[^,]{2,40})$", before):
            venue["address"] = a.group(1).strip()
    return venue


# --------------------------------------------------------------------------- sestavení dat
def our_position(standings: list[dict]) -> int | None:
    return next((r["pos"] for r in standings if r["name"] == TEAM_NAME), None)


def archive_season(data: dict) -> dict:
    pos, count = our_position(data.get("standings", [])), len(data.get("standings", []))
    season = data.get("season", {})
    return {
        "season": season.get("label", ""), "league": season.get("league", ""), "final": True,
        "note": f"{TEAM_NAME} skončilo na {pos}. místě z {count} týmů." if pos else "",
        "standings": data.get("standings", []), "players": data.get("players", []),
    }


def next_label(label: str) -> str:
    m = re.fullmatch(r"(\d{4})/(\d{4})", label or "")
    return f"{int(m.group(1)) + 1}/{int(m.group(2)) + 1}" if m else label


class Sync:
    """Postupně aktualizuje jednotlivé části dat a eviduje, co se změnilo."""

    def __init__(self, prev: dict, config: dict, fetcher: Fetcher) -> None:
        self.prev = prev
        self.data = copy.deepcopy(prev)
        self.config = config
        self.f = fetcher
        self.report: list[tuple[str, str]] = []     # (část, stav)
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.pages_ok = 0

    def get(self, url: str) -> str | None:
        html = self.f.get(url)
        self.pages_ok += html is not None
        return html

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def set(self, key: str, value, label: str) -> None:
        changed = self.data.get(key) != value
        self.data[key] = value
        self.report.append((label, "aktualizováno" if changed else "beze změny"))

    def run(self) -> dict:
        self.data.setdefault("season", {})
        for key, default in (("history", []), ("venues", {}), ("leagueTeams", {}), ("roster", {})):
            self.data.setdefault(key, default)
        team_page = self.sync_season()
        self.sync_standings()
        self.sync_schedule()
        self.sync_players(team_page)
        self.sync_roster()
        self.sync_venues()
        return self.data

    # -- sezóna ------------------------------------------------------------
    def sync_season(self) -> str | None:
        html = self.get(page_url(page="statistika-hracu", league_type=LEAGUE_TYPE, team=TEAM_ID))
        found = parse_season(html) if html else {}
        season = self.data["season"]
        if not found.get("leagueId") or not found.get("leagueTeamId"):
            self.warn("Nepodařilo se zjistit aktuální sezónu, použijí se dosavadní ID.")
            self.report.append(("Sezóna", "dosavadní"))
            return html
        new_id = found.get("id") and season.get("id") and found["id"] != season["id"]
        new_team = season.get("leagueTeamId") and found["leagueTeamId"] != season["leagueTeamId"]
        if new_id or new_team:                       # nová sezóna = nové ID sezóny nebo nové ID družstva
            self.data["history"].insert(0, archive_season(self.data))
            self.notes.append(f"Nová sezóna {found.get('label') or next_label(season.get('label', ''))} – "
                              f"sezóna {season.get('label')} přesunuta do historie.")
            for key in ("standings", "matches", "players", "leagueTeams"):
                self.data[key] = [] if key != "leagueTeams" else {}
            season["final"] = False
            found.setdefault("label", next_label(season.get("label", "")))
        season.update({k: v for k, v in found.items() if v})
        self.report.append(("Sezóna", f"{season.get('label', '?')} · {season.get('league', '?')}"))
        return html

    # -- tabulka -----------------------------------------------------------
    def sync_standings(self) -> None:
        html = self.get(page_url(page="ligova-skupina", league=self.data["season"]["leagueId"]))
        rows, final = parse_standings(html) if html else ([], False)
        if len(rows) < 6 or our_position(rows) is None:
            self.warn("Tabulku se nepodařilo načíst, zůstává poslední platná.")
            self.report.append(("Tabulka", "chyba – ponechána stará"))
            return
        self.data["season"]["final"] = final
        self.set("standings", rows, "Tabulka")

    # -- rozpis a výsledky ---------------------------------------------------
    def sync_schedule(self) -> None:
        s = self.data["season"]
        html = self.get(page_url(page="rozpis-utkani", league_type=LEAGUE_TYPE, season=s.get("id", ""),
                                 league=s["leagueId"], team=TEAM_ID, round=""))
        matches = parse_schedule(html) if html else []
        if len(matches) < max(5, len(self.data.get("matches", [])) // 2):   # málo zápasů = stránka se nenačetla celá
            self.warn("Rozpis zápasů se nepodařilo načíst, zůstává poslední platný.")
            self.report.append(("Rozpis a výsledky", "chyba – ponechán starý"))
            return
        old = {m["round"]: m for m in self.data.get("matches", [])}
        for m in matches:
            o = old.get(m["round"])
            if o and (o["date"], o["time"], o.get("place")) != (m["date"], m["time"], m["place"]):
                self.notes.append(f"Kolo {m['round']}: změna termínu nebo místa "
                                  f"({o['date']} {o['time']}, {o.get('venue')} → {m['date']} {m['time']}, {m['venue']})")
            if m["result"] and not (o and o.get("result")):
                self.notes.append(f"Kolo {m['round']}: výsledek {m['home']} {m['score']} {m['away']}")
        self.set("matches", matches, "Rozpis a výsledky")

    # -- hráči -------------------------------------------------------------
    def sync_players(self, team_page: str | None) -> None:
        s = self.data["season"]
        ours = parse_players(team_page) if team_page else []
        html = self.get(page_url(page="statistika-hracu", league=s["leagueId"]))
        league = parse_players(html) if html else []
        if not ours:
            ours = [p for p in league if p.get("lt") == s.get("leagueTeamId")]
        for p in ours:
            p.pop("lt", None)
        if ours:
            self.set("players", sorted(ours, key=lambda p: p["name"]), "Statistika našich hráčů")
        else:
            self.warn("Statistiku našich hráčů se nepodařilo načíst.")
            self.report.append(("Statistika našich hráčů", "chyba – ponechána stará"))

        if not league:
            self.warn("Statistiku hráčů ligy se nepodařilo načíst, soupisky soupeřů zůstávají.")
            self.report.append(("Hráči soupeřů", "chyba – ponecháni staří"))
            return
        teams = {}
        for row in self.data.get("standings", []):
            members = [{"name": p["name"], "lkh": p["lkh"], "zo": p["zo"], "zv": p["zv"]}
                       for p in league if p.get("lt") == row["lt"]]
            if members or row["lt"] not in self.data["leagueTeams"]:
                teams[row["lt"]] = {"name": row["name"], "players": members}
            else:
                teams[row["lt"]] = self.data["leagueTeams"][row["lt"]]
        self.set("leagueTeams", teams, "Hráči soupeřů")

    # -- soupiska ----------------------------------------------------------
    def sync_roster(self) -> None:
        lt = self.data["season"].get("leagueTeamId")
        html = self.get(page_url(page="profil-druzstva", league_team=lt)) if lt else None
        members, captain = parse_roster(html) if html else ([], None)
        if len(members) < max(3, len(self.data.get("roster", {}).get("members", [])) // 2):
            self.warn("Soupisku se nepodařilo načíst, tým zůstává beze změny.")
            self.report.append(("Soupiska", "chyba – ponechána stará"))
            return
        old = set(self.data.get("roster", {}).get("members", []))
        for name in members:
            if old and name not in old:
                self.notes.append(f"Nový hráč na soupisce: {name}")
        for name in sorted(old - set(members)):
            self.notes.append(f"Ze soupisky odešel: {name}")
        self.set("roster", {"members": members, "captain": captain}, "Soupiska")

    # -- hrací místa -------------------------------------------------------
    def sync_venues(self) -> None:
        known = set(self.config.get("venues", {})) | set(self.data["venues"])
        missing = sorted({m["place"] for m in self.data.get("matches", []) if m.get("place")} - known)
        added = 0
        for place in missing[:MAX_NEW_VENUES_PER_RUN]:
            html = self.get(page_url(page="hraci-misto", place=place))
            venue = parse_venue(html) if html else {}
            if "lat" in venue:
                label = next(m["venue"] for m in self.data["matches"] if m["place"] == place)
                self.data["venues"][place] = {"label": label, **venue}
                self.notes.append(f"Nové hrací místo: {label} ({venue.get('address', 'bez adresy')})")
                added += 1
            else:
                self.warn(f"Hrací místo {place} se nepodařilo dohledat, mapa ho najde jen podle názvu.")
        self.report.append(("Hrací místa", f"{added} nových" if missing else "beze změny"))


# --------------------------------------------------------------------------- index.html
def read_island(html: str) -> tuple[dict, dict]:
    data = DATA_ISLAND.search(html)
    config = CONFIG_ISLAND.search(html)
    if not data:
        raise SystemExit(f"V {INDEX_FILE} chybí blok <script id=\"site-data\">.")
    return json.loads(data.group(2)), json.loads(config.group(1)) if config else {}


def write_island(html: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return DATA_ISLAND.sub(lambda m: m.group(1) + payload + m.group(3), html, count=1)


def without_timestamp(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "updated"}


def write_summary(sync: Sync, changed: bool) -> None:
    lines = [f"### Roshambo Praha – synchronizace ({datetime.now(TZ):%d. %m. %Y %H:%M})", "",
             "| Část | Stav |", "|---|---|", *[f"| {a} | {b} |" for a, b in sync.report], ""]
    lines.append("**Web byl aktualizován.**" if changed else "Žádné změny, web zůstává.")
    if sync.notes:
        lines += ["", "**Změny:**", *[f"- {n}" for n in sync.notes]]
    if sync.warnings:
        lines += ["", "**Varování:**", *[f"- {w}" for w in sync.warnings]]
    text = "\n".join(lines)
    print(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if os.environ.get("GITHUB_ACTIONS"):
        for w in sync.warnings:
            print(f"::warning::{w}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    html = INDEX_FILE.read_text(encoding="utf-8")
    prev, config = read_island(html)
    sync = Sync(prev, config, Fetcher(os.environ.get("SCRAPERAPI_KEY")))
    data = sync.run()

    changed = without_timestamp(data) != without_timestamp(prev)
    if changed:
        data["updated"] = datetime.now(TZ).isoformat(timespec="minutes")
        INDEX_FILE.write_text(write_island(html, data), encoding="utf-8")
    write_summary(sync, changed)

    if sync.pages_ok == 0:
        print("::error::Ze sipky.org se nepodařilo stáhnout žádnou stránku (zkontroluj ScraperAPI).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
