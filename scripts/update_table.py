#!/usr/bin/env python3
"""
Stáhne aktuální tabulku 3. ligy C (sezóna 2026/2027) ze sipky.org
a aktualizuje tabulku v index.html na místě označeném ID atributy:
  - <span id="current-season-meta">      ... krátký popisek (X. místo)
  - <tbody id="current-season-tbody">    ... řádky tabulky
  - <div id="current-season-legend">     ... vysvětlující věta pod tabulkou

Skript nic nemění, pokud sezóna ještě nezačala (na sipky.org zatím
není žádná odehraná kola / tabulka).
"""

import re
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

LEAGUE_URL = "https://www.sipky.org/?region=stc&page=ligova-skupina&league=228360"
OUR_TEAM = "Roshambo Praha"
INDEX_FILE = "index.html"
SEASON_LABEL = "3. liga C"


def parse_html_standings(html):
    soup = BeautifulSoup(html, "html.parser")

    standings_table = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if cells and re.match(r"^\d+\.$", cells[0].get_text(strip=True)):
                standings_table = table
                break
        if standings_table:
            break

    if standings_table is None:
        return None

    teams = []
    for row in standings_table.find_all("tr"):
        cells = row.find_all("td")
        if not cells or not re.match(r"^\d+\.$", cells[0].get_text(strip=True)):
            continue
        try:
            pos = cells[0].get_text(strip=True)
            name = cells[1].get_text(strip=True)
            kol = cells[2].get_text(strip=True)
            v = cells[3].get_text(strip=True)
            r = cells[5].get_text(strip=True)
            p = cells[7].get_text(strip=True)
            skore_h = cells[9].get_text(strip=True)
            skore_a = cells[11].get_text(strip=True)
            legy_h = cells[12].get_text(strip=True)
            legy_a = cells[14].get_text(strip=True)
            body = cells[15].get_text(strip=True)
        except IndexError:
            continue
        teams.append({
            "pos": pos, "name": name, "kol": kol, "v": v, "r": r, "p": p,
            "skore": f"{skore_h}:{skore_a}", "legy": f"{legy_h}:{legy_a}", "body": body,
        })
    return teams


def parse_markdown_standings(md_text):
    """Parsuje tabulku ze stránky převedené na markdown (přes Jina Reader).
    Řádky standings tabulky mají přesně 16 sloupců; ostatní tabulky na
    stránce (např. statistika hráčů) mají jiný počet, takže se samy
    přeskočí."""
    link_pattern = re.compile(r"\[([^\]]*)\]\([^)]*\)")
    teams = []
    for line in md_text.splitlines():
        line = line.strip()
        if not re.match(r"^\|\s*\d+\.\s*\|", line):
            continue
        clean = link_pattern.sub(r"\1", line)
        cells = [c.strip() for c in clean.split("|")]
        # odstraň prázdné krajní buňky vzniklé úvodní/koncovou svislítkem
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) != 16:
            continue
        try:
            pos = cells[0]
            name = cells[1]
            kol = cells[2]
            v = cells[3]
            r = cells[5]
            p = cells[7]
            skore_h = cells[9]
            skore_a = cells[11]
            legy_h = cells[12]
            legy_a = cells[14]
            body = cells[15]
        except IndexError:
            continue
        teams.append({
            "pos": pos, "name": name, "kol": kol, "v": v, "r": r, "p": p,
            "skore": f"{skore_h}:{skore_a}", "legy": f"{legy_h}:{legy_a}", "body": body,
        })
    return teams if teams else None


def fetch_standings():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    # Pokus 1: přímý přístup (funguje z běžného prohlížeče, ale sipky.org
    # blokuje datacentrové IP adresy GitHub Actions runnerů - proto 403).
    try:
        resp = session.get(LEAGUE_URL, timeout=20)
        resp.raise_for_status()
        html = resp.content.decode("cp1250", errors="replace")
        teams = parse_html_standings(html)
        if teams is not None:
            return teams
        # HTML se stáhlo, ale tabulka tam není -> sezóna asi nezačala
        return None
    except requests.RequestException:
        pass

    # Pokus 2: ScraperAPI (placená/bezplatná scraping služba s rotujícími
    # IP adresami - spolehlivěji obchází ochranu proti botům). Použije se
    # jen pokud je nastavený tajný klíč SCRAPERAPI_KEY.
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if api_key:
        try:
            import urllib.parse
            scraper_url = (
                "https://api.scraperapi.com?api_key=" + api_key +
                "&url=" + urllib.parse.quote(LEAGUE_URL, safe="")
            )
            resp = session.get(scraper_url, timeout=60)
            resp.raise_for_status()
            html = resp.content.decode("cp1250", errors="replace")
            teams = parse_html_standings(html)
            if teams is not None:
                return teams
            return None
        except requests.RequestException:
            pass

    # Pokus 3: Jina AI Reader - přečte stránku ze své vlastní infrastruktury
    # (jiná IP adresa než GitHub Actions) a vrátí obsah jako čistý text
    # včetně tabulek v markdown formátu.
    try:
        jina_url = "https://r.jina.ai/" + LEAGUE_URL
        resp = session.get(jina_url, timeout=45)
        resp.raise_for_status()
        teams = parse_markdown_standings(resp.text)
        return teams
    except requests.RequestException as e:
        raise RuntimeError(
            f"Nepodařilo se stáhnout stránku ani přímo, ani přes Jina Reader. "
            f"Poslední chyba: {e}"
        )


def build_tbody(teams):
    rows = []
    for t in teams:
        cls = ""
        if t["name"] == OUR_TEAM:
            cls = ' class="us"'
        elif t["pos"] in ("1.", "2.", "3."):
            cls = ' class="podium"'
        rows.append(
            f'<tr{cls}><td class="pos">{t["pos"]}</td>'
            f'<td class="team-name">{t["name"]}</td>'
            f'<td>{t["kol"]}</td><td>{t["v"]}</td><td>{t["r"]}</td><td>{t["p"]}</td>'
            f'<td>{t["skore"]}</td><td>{t["legy"]}</td>'
            f'<td class="pts">{t["body"]}</td></tr>'
        )
    return "\n            ".join(rows)


def czech_now_str():
    # Přibližný český čas (bez ohledu na DST, jen pro info v textu)
    cz = datetime.now(timezone.utc) + timedelta(hours=1)
    return cz.strftime("%d.%m.%Y %H:%M")


def update_index_html(teams):
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    us = next((t for t in teams if t["name"] == OUR_TEAM), None)
    total = len(teams)

    if us:
        meta_text = f"{SEASON_LABEL} · {us['pos']} místo"
        legend_text = (
            f'Průběžné pořadí — Roshambo Praha je na <b>{us["pos"]} místě</b> '
            f'z {total} týmů. Naposledy aktualizováno automaticky {czech_now_str()}.'
        )
    else:
        meta_text = f"{SEASON_LABEL} · průběžné pořadí"
        legend_text = (
            f'Průběžné pořadí sezóny {SEASON_LABEL}. '
            f'Naposledy aktualizováno automaticky {czech_now_str()}.'
        )

    new_tbody = build_tbody(teams)

    html = re.sub(
        r'(<span class="season-acc-meta" id="current-season-meta">)(.*?)(</span>)',
        lambda m: m.group(1) + meta_text + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'(<tbody id="current-season-tbody">)(.*?)(</tbody>)',
        lambda m: m.group(1) + "\n            " + new_tbody + "\n          " + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'(<div class="standings-legend" id="current-season-legend">)(.*?)(</div>)',
        lambda m: m.group(1) + legend_text + m.group(3),
        html, count=1, flags=re.DOTALL,
    )

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    teams = fetch_standings()
    if not teams:
        print("Sezóna zatím nemá odehraná kola — tabulka se nemění.")
        sys.exit(0)
    update_index_html(teams)
    print(f"Tabulka aktualizována, {len(teams)} týmů.")


if __name__ == "__main__":
    main()
