"""Esporta il contenuto del DB in JSON statici consumabili da GitHub Pages.

Genera in docs/data/:
  bandi.json        perimetro utile (aperti + rilevanti per Comuni), payload principale
  archivio.json     tutto il resto, caricato on-demand dal frontend
  meta.json         statistiche, elenco fonti, esito ultimo run
  last_run.json     timestamp (garantisce un commit giornaliero -> cron sempre attivo)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core import connect

DOCS = Path(__file__).resolve().parent.parent / "docs" / "data"
SOGLIA_RILEVANZA = 2.0
STATI_ATTIVI = ("aperto", "in scadenza", "programmato")

CAMPI = ("uid", "titolo", "url", "fonte_nome", "livello", "regione", "ente",
         "descrizione", "data_apertura", "data_scadenza", "dotazione",
         "beneficiari", "tema", "score_comuni", "score_bando", "stato")


def _riga(r) -> dict:
    d = {k: r[k] for k in CAMPI}
    d["descrizione"] = (d["descrizione"] or "")[:600]
    try:
        d["beneficiari"] = json.loads(d["beneficiari"] or "[]")
    except json.JSONDecodeError:
        d["beneficiari"] = []
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def esporta(soglia: float = SOGLIA_RILEVANZA) -> dict:
    DOCS.mkdir(parents=True, exist_ok=True)
    con = connect()
    ph = ",".join("?" * len(STATI_ATTIVI))

    utili = [_riga(r) for r in con.execute(
        f"SELECT * FROM bandi WHERE stato IN ({ph}) AND score_comuni >= ? AND score_bando >= 0 "
        f"ORDER BY (data_scadenza IS NULL), data_scadenza, score_comuni DESC",
        (*STATI_ATTIVI, soglia))]

    archivio = [_riga(r) for r in con.execute(
        f"SELECT * FROM bandi WHERE NOT (stato IN ({ph}) AND score_comuni >= ? AND score_bando >= 0) "
        f"ORDER BY (data_scadenza IS NULL), data_scadenza DESC LIMIT 5000",
        (*STATI_ATTIVI, soglia))]

    fonti = [dict(r) for r in con.execute(
        "SELECT fonte_id, fonte_nome, livello, regione, COUNT(*) n_totali,"
        " SUM(CASE WHEN stato IN ('aperto','in scadenza','programmato')"
        "      AND score_comuni >= ? AND score_bando >= 0 THEN 1 ELSE 0 END) n_utili"
        " FROM bandi GROUP BY fonte_id ORDER BY n_utili DESC", (soglia,))]

    log = [dict(r) for r in con.execute(
        "SELECT fonte_id, esito, n_trovati, n_nuovi, messaggio FROM ingest_log"
        " WHERE ts >= (SELECT MAX(ts) FROM ingest_log) OR ts >= datetime('now','-1 day')"
        " ORDER BY ts DESC LIMIT 40")]

    faccette = {}
    for campo in ("regione", "livello", "tema", "stato"):
        faccette[campo] = {r[0]: r[1] for r in con.execute(
            f"SELECT COALESCE({campo},'n.d.'), COUNT(*) FROM bandi"
            f" WHERE stato IN ({ph}) AND score_comuni >= ? AND score_bando >= 0"
            f" GROUP BY 1 ORDER BY 2 DESC", (*STATI_ATTIVI, soglia))}

    ora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {
        "generato": ora,
        "soglia_rilevanza": soglia,
        "totali": {"in_database": con.execute("SELECT COUNT(*) FROM bandi").fetchone()[0],
                   "perimetro_utile": len(utili), "archivio": len(archivio)},
        "faccette": faccette,
        "fonti": fonti,
        "ultimo_ingest": log,
    }

    scritti = {}
    for nome, dati in (("bandi", utili), ("archivio", archivio), ("meta", meta),
                       ("last_run", {"ts": ora})):
        p = DOCS / f"{nome}.json"
        p.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")))
        scritti[nome] = (len(dati) if isinstance(dati, list) else 1, p.stat().st_size)
    return {"meta": meta, "file": scritti}


if __name__ == "__main__":
    r = esporta()
    print(f"{'FILE':<16}{'RECORD':>8}{'KB':>9}")
    print("-" * 34)
    for nome, (n, size) in r["file"].items():
        print(f"{nome + '.json':<16}{n:>8}{size / 1024:>8.1f}")
    t = r["meta"]["totali"]
    print("-" * 34)
    print(f"DB {t['in_database']} record | perimetro utile {t['perimetro_utile']}")
