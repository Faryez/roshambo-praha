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
TEAM_MATCHES_URL = "https://www.sipky.org/?region=stc&page=rozpis-utkani&league_team=2745352&played=1"
PLAYER_STATS_URL = "https://www.sipky.org/?region=stc&page=statistika-hracu&league_team=2745352"
OUR_TEAM = "Roshambo Praha"
INDEX_FILE = "index.html"
SEASON_LABEL = "3. liga C"
CURRENT_SEASON_LABEL = "2026/2027"


def decode_smart(content):
    """Stránka sipky.org je ve windows-1250, ale některé proxy služby
    (např. ScraperAPI) ji před vrácením samy převedou na UTF-8. Zkusíme
    proto nejdřív UTF-8 - pokud selže, je to opravdu windows-1250."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1250", errors="replace")


def parse_html_standings(html):
    soup = BeautifulSoup(html, "html.parser")

    # Sipky.org sama mění nadpis nad tabulkou z "Tabulka - průběžné pořadí"
    # na "Tabulka - konečné pořadí", jakmile je sezóna u konce - podle
    # toho poznáme, jestli ještě probíhá, nebo už skončila.
    is_final = "konečné pořadí" in html.lower()

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
        return None, is_final

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
    return teams, is_final


def parse_markdown_standings(md_text):
    """Parsuje tabulku ze stránky převedené na markdown (přes Jina Reader).
    Řádky standings tabulky mají přesně 16 sloupců; ostatní tabulky na
    stránce (např. statistika hráčů) mají jiný počet, takže se samy
    přeskočí."""
    is_final = "konečné pořadí" in md_text.lower()
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
    return (teams if teams else None), is_final


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
        html = decode_smart(resp.content)
        teams, is_final = parse_html_standings(html)
        if teams is not None:
            return teams, is_final
        # HTML se stáhlo, ale tabulka tam není -> sezóna asi nezačala
        return None, is_final
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
            html = decode_smart(resp.content)
            teams, is_final = parse_html_standings(html)
            if teams is not None:
                return teams, is_final
            return None, is_final
        except requests.RequestException:
            pass

    # Pokus 3: Jina AI Reader - přečte stránku ze své vlastní infrastruktury
    # (jiná IP adresa než GitHub Actions) a vrátí obsah jako čistý text
    # včetně tabulek v markdown formátu.
    try:
        jina_url = "https://r.jina.ai/" + LEAGUE_URL
        resp = session.get(jina_url, timeout=45)
        resp.raise_for_status()
        teams, is_final = parse_markdown_standings(resp.text)
        return teams, is_final
    except requests.RequestException as e:
        raise RuntimeError(
            f"Nepodařilo se stáhnout stránku ani přímo, ani přes Jina Reader. "
            f"Poslední chyba: {e}"
        )


def fetch_html_with_fallback(url, session):
    """Stáhne stránku přímo, a když to selže, zkusí ScraperAPI (pokud je
    nastavený klíč). Vrátí HTML text, nebo None při úplném selhání."""
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return decode_smart(resp.content)
    except requests.RequestException:
        pass

    api_key = os.environ.get("SCRAPERAPI_KEY")
    if not api_key:
        return None
    try:
        import urllib.parse
        scraper_url = (
            "https://api.scraperapi.com?api_key=" + api_key +
            "&url=" + urllib.parse.quote(url, safe="")
        )
        resp = session.get(scraper_url, timeout=60)
        resp.raise_for_status()
        return decode_smart(resp.content)
    except requests.RequestException:
        return None


def fetch_roshambo_results(session):
    """Stáhne odehrané zápasy Roshambo Praha a pro každý určí, jestli šlo
    o výhru/remízu/prohru. Vrátí slovník {"YYYY-MM-DD": "win"/"draw"/"loss"}.
    Při jakémkoli problému vrátí prázdný slovník - tahle funkce nesmí shodit
    zbytek skriptu, protože jde jen o doplňkovou vizuální informaci."""
    try:
        html = fetch_html_with_fallback(TEAM_MATCHES_URL, session)
        if not html:
            return {}
        soup = BeautifulSoup(html, "html.parser")
        date_re = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
        results = {}

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            texts = [c.get_text(strip=True) for c in cells]

            date_idx = next((i for i, t in enumerate(texts) if date_re.match(t)), None)
            if date_idx is None:
                continue
            if not any(OUR_TEAM in t for t in texts):
                continue
            if ":" not in texts:
                continue

            colon_idx = texts.index(":")
            try:
                skore_home = int(texts[colon_idx - 1])
                skore_away = int(texts[colon_idx + 1])
            except (ValueError, IndexError):
                continue

            rosh_idx = next((i for i, t in enumerate(texts) if OUR_TEAM in t), None)
            if rosh_idx is None:
                continue

            is_home = rosh_idx < colon_idx
            rosh_score, opp_score = (skore_home, skore_away) if is_home else (skore_away, skore_home)

            if rosh_score > opp_score:
                result = "win"
            elif rosh_score < opp_score:
                result = "loss"
            else:
                result = "draw"

            d, mo, y = date_re.match(texts[date_idx]).groups()
            iso = f"{y}-{int(mo):02d}-{int(d):02d}"
            results[iso] = result

        return results
    except Exception:
        return {}


def fetch_player_stats(session):
    """Stáhne statistiku hráčů Roshambo Praha pro AKTUÁLNÍ sezónu ze
    stránky 'statistika-hracu' a pro každého spočítá:
      - Úspěšnost = Vyhráno / Odehráno * 100
      - Prospěšnost = (Vyhráno / Odehráno) * Vyhráno   (vzorec dle ČFO)
    Vrátí seznam slovníků seřazený sestupně podle Prospěšnosti.
    Při jakémkoli problému vrátí prázdný seznam - je to jen doplňková
    informace, nesmí shodit zbytek skriptu."""
    try:
        html = fetch_html_with_fallback(PLAYER_STATS_URL, session)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")

        table = None
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            for row in rows:
                cells = row.find_all("td")
                if cells and re.match(r"^\d+\.$", cells[0].get_text(strip=True)):
                    table = t
                    break
            if table:
                break
        if table is None:
            return []

        players = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells or not re.match(r"^\d+\.$", cells[0].get_text(strip=True)):
                continue
            # sloupce: Pořadí, Hráč, LKH, %, C, O, V, %, O, V, ... (Zápasy O/V jsou sloupce indexy 5 a 6)
            try:
                name = cells[1].get_text(strip=True)
                o = int(cells[5].get_text(strip=True))
                v = int(cells[6].get_text(strip=True))
            except (IndexError, ValueError):
                continue
            if o == 0:
                continue
            uspesnost = v / o * 100
            prospesnost = (v / o) * v
            players.append({
                "name": name,
                "uspesnost": uspesnost,
                "prospesnost": prospesnost,
            })

        players.sort(key=lambda p: p["prospesnost"], reverse=True)
        return players
    except Exception:
        return []


def build_mini_stats_tbody(players):
    rows = []
    for p in players:
        usp = f'{p["uspesnost"]:.2f}'.replace(".", ",") + " %"
        prosp = f'{p["prospesnost"]:.2f}'.replace(".", ",")
        rows.append(
            f'<tr><td>{p["name"]}</td><td class="msu">{usp}</td><td>{prosp}</td></tr>'
        )
    return "\n".join(rows)


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
            f'<td class="pts">{t["body"]}</td>'
            f'<td>{t["kol"]}</td><td>{t["v"]}</td><td>{t["r"]}</td><td>{t["p"]}</td>'
            f'<td>{t["legy"]}</td><td>{t["skore"]}</td></tr>'
        )
    return "\n            ".join(rows)


def czech_now_str():
    # Přibližný český čas (bez ohledu na DST, jen pro info v textu)
    cz = datetime.now(timezone.utc) + timedelta(hours=1)
    return cz.strftime("%d.%m.%Y %H:%M")


def update_index_html(teams, is_final, match_results=None, player_stats=None):
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    us = next((t for t in teams if t["name"] == OUR_TEAM), None)
    total = len(teams)

    if is_final:
        if us:
            meta_text = f"Sezóna {CURRENT_SEASON_LABEL} · {us['pos']} místo"
            inline_meta_text = f"{SEASON_LABEL} · {us['pos']} místo"
            legend_text = (
                f'Sezóna skončila — Roshambo Praha se umístilo na <b>{us["pos"]} místě</b> '
                f'z {total} týmů. Naposledy aktualizováno automaticky {czech_now_str()}.'
            )
        else:
            meta_text = f"Sezóna {CURRENT_SEASON_LABEL} · konečné pořadí"
            inline_meta_text = f"{SEASON_LABEL} · konečné pořadí"
            legend_text = (
                f'Sezóna {SEASON_LABEL} skončila. '
                f'Naposledy aktualizováno automaticky {czech_now_str()}.'
            )
    else:
        if us:
            meta_text = f"Sezóna {CURRENT_SEASON_LABEL} · průběžně {us['pos']} místo"
            inline_meta_text = f"{SEASON_LABEL} · průběžně {us['pos']} místo"
            legend_text = (
                f'Průběžné pořadí — Roshambo Praha je na <b>{us["pos"]} místě</b> '
                f'z {total} týmů. Naposledy aktualizováno automaticky {czech_now_str()}.'
            )
        else:
            meta_text = f"Sezóna {CURRENT_SEASON_LABEL} · průběžné pořadí"
            inline_meta_text = f"{SEASON_LABEL} · průběžné pořadí"
            legend_text = (
                f'Průběžné pořadí sezóny {SEASON_LABEL}. '
                f'Naposledy aktualizováno automaticky {czech_now_str()}.'
            )

    new_tbody = build_tbody(teams)

    html = re.sub(
        r'(<option value="2026" id="current-season-meta">)(.*?)(</option>)',
        lambda m: m.group(1) + meta_text + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'(<div class="season-acc-meta" id="current-season-meta-inline"[^>]*>)(.*?)(</div>)',
        lambda m: m.group(1) + inline_meta_text + m.group(3),
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

    if match_results:
        for iso, result in match_results.items():
            pattern = re.compile(r'(<tr data-date="' + re.escape(iso) + r'")([^>]*)(>)')

            def repl(m, result=result):
                attrs = re.sub(r'\s*data-result="[^"]*"', '', m.group(2))
                return m.group(1) + attrs + f' data-result="{result}"' + m.group(3)

            html = pattern.sub(repl, html, count=1)

    if player_stats:
        new_mini_tbody = build_mini_stats_tbody(player_stats)
        html = re.sub(
            r'(<tbody id="current-season-mini-stats-tbody">)(.*?)(</tbody>)',
            lambda m: m.group(1) + "\n" + new_mini_tbody + "\n" + m.group(3),
            html, count=1, flags=re.DOTALL,
        )

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    teams, is_final = fetch_standings()
    if not teams:
        print("Sezóna zatím nemá odehraná kola — tabulka se nemění.")
        sys.exit(0)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    })
    match_results = fetch_roshambo_results(session)
    player_stats = fetch_player_stats(session)

    update_index_html(teams, is_final, match_results, player_stats)
    stav = "KONEČNÉ" if is_final else "průběžné"
    print(
        f"Tabulka aktualizována ({stav}), {len(teams)} týmů, "
        f"{len(match_results)} výsledků zápasů, {len(player_stats)} hráčů ve statistice."
    )


if __name__ == "__main__":
    main()
