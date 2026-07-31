"""Arricchimento delle schede: recupera scadenza e dotazione dalla pagina di dettaglio.

Non e' un estrattore generico. Un regex universale su pagine della PA produce
soprattutto falsi positivi (date di rendicontazione, importi gia' liquidati,
termini di altri procedimenti citati nel testo). Qui ogni fonte ha il suo
estrattore, scritto sulla struttura reale di quelle pagine, e si arricchisce solo
cio' che risulta ancora 'ignota'.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from selectolax.parser import HTMLParser

from core import Http, connect, parse_data, parse_importo

# fonti per cui esiste un estrattore verificato
ESTRATTORI = {}


def estrattore(fonte_id):
    def dec(fn):
        ESTRATTORI[fonte_id] = fn
        return fn
    return dec


def _testo(html: str) -> str:
    d = HTMLParser(html)
    for t in d.css("script,style,nav,header,footer,aside"):
        t.decompose()
    nodo = d.css_first("main") or d.css_first("article") or d.body
    return re.sub(r"\s+", " ", nodo.text(separator=" ", strip=True)) if nodo else ""


@estrattore("pie-bandi")
def _piemonte(html: str) -> dict:
    """Le schede Piemonte espongono 'Scadenza <gg>, dd/mm/aaaa - hh:mm'."""
    t = _testo(html)
    out = {}
    m = re.search(r"Scadenza\s+(?:\w{3},\s*)?(\d{2}/\d{2}/\d{4})", t)
    if m:
        out["data_scadenza"] = parse_data(m.group(1))
    return out


@estrattore("cal-europa")
def _calabria(html: str) -> dict:
    """Le pagine Calabria Europa dichiarano dotazione e termini in prosa."""
    t = _testo(html)
    out = {}
    m = re.search(r"dotazione finanziaria[^.]{0,40}?(?:pari a|di)\s*(?:euro\s*)?"
                  r"([\d.,]+(?:\s*(?:milioni|mln))?)", t, re.I)
    if m:
        out["dotazione"] = parse_importo(m.group(1))
    m = re.search(r"(?:scadenz\w+|termine\w*|entro)\D{0,40}(\d{1,2}\s+"
                  r"(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
                  r"settembre|ottobre|novembre|dicembre)\s+\d{4})", t, re.I)
    if not m:
        m = re.search(r"(?:scadenz\w+|entro il)\D{0,25}(\d{1,2}/\d{1,2}/\d{4})", t, re.I)
    if m:
        out["data_scadenza"] = parse_data(m.group(1))
    return out


def _plausibile(iso: str | None) -> bool:
    """Scarta date assurde: una scadenza nel passato remoto o oltre 3 anni non e' un termine."""
    if not iso:
        return False
    try:
        d = datetime.fromisoformat(iso).date()
    except ValueError:
        return False
    return date.today().replace(year=date.today().year - 1) <= d <= date.today().replace(year=date.today().year + 3)


def arricchisci(limite: int = 60, verbose: bool = True) -> dict:
    con = connect()
    http = Http(delay=2.0)
    fonti = ",".join(f"'{f}'" for f in ESTRATTORI)
    righe = list(con.execute(f"""
        SELECT uid, fonte_id, url, titolo, dotazione FROM bandi
        WHERE scadenza_stato = 'ignota' AND fonte_id IN ({fonti})
          AND stato IN ('aperto','in scadenza','programmato')
          AND score_comuni >= 2 AND score_bando >= 0
        LIMIT ?""", (limite,)))

    esiti = {"esaminati": 0, "scadenze": 0, "dotazioni": 0, "errori": 0}
    for r in righe:
        esiti["esaminati"] += 1
        try:
            html = http.get(r["url"]).text
            dati = ESTRATTORI[r["fonte_id"]](html)
        except Exception as e:
            esiti["errori"] += 1
            if verbose:
                print(f"  ERRORE {r['fonte_id']}: {type(e).__name__}")
            continue

        scad = dati.get("data_scadenza")
        if scad and _plausibile(scad):
            con.execute("UPDATE bandi SET data_scadenza=?, scadenza_stato='data' WHERE uid=?",
                        (scad, r["uid"]))
            esiti["scadenze"] += 1
            if verbose:
                print(f"  + scadenza {scad}  {r['titolo'][:52]}")
        if dati.get("dotazione") and r["dotazione"] is None:
            con.execute("UPDATE bandi SET dotazione=? WHERE uid=?", (dati["dotazione"], r["uid"]))
            esiti["dotazioni"] += 1
    con.commit()

    # ricalcola lo stato: una scadenza appena trovata puo' aver chiuso il bando
    con.execute("""UPDATE bandi SET stato='chiuso'
                   WHERE data_scadenza IS NOT NULL AND data_scadenza < date('now')
                     AND stato != 'chiuso'""")
    con.commit()
    return esiti


if __name__ == "__main__":
    e = arricchisci()
    print(f"\nesaminati {e['esaminati']} | scadenze trovate {e['scadenze']} | "
          f"dotazioni {e['dotazioni']} | errori {e['errori']}")
