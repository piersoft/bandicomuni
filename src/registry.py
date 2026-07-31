"""Registro delle fonti e orchestratore dell'ingestione giornaliera."""
from __future__ import annotations

import sys
import traceback
import warnings

from connectors import (PaDigitaleConnettore, CkanDiscovery, PloneConnettore, RssConnettore,
                        SediaConnettore, SocrataConnettore, PugliaConnettore)
from html_connector import HtmlConnettore
from core import Http, connect, log_ingest, salva

warnings.filterwarnings("ignore")

# Perimetro del progetto: bandi nazionali, europei e della sola Regione Puglia.
# Le fonti delle altre regioni restano configurate ma disattivate (attiva=False):
# riattivarle e' questione di togliere il flag, non di riscrivere il connettore.
FONTI = [
    # --- Fonti nazionali (HTML) ---------------------------------------------
    (HtmlConnettore, dict(
        id="dpcoe", nome="Dip. Politiche di Coesione", livello="nazionale",
        ente="Presidenza del Consiglio - Dip. per le politiche di coesione",
        lista=["https://politichecoesione.governo.it/it/finanziamenti-avvisi-e-bandi/",
               "https://politichecoesione.governo.it/it/documenti-ed-esiti-istituzionali/"
               "documenti-di-attuazione-dei-finanziamenti-avvisi-e-bandi/"],
        sel_item=".card", sel_titolo="h2 a, h3 a")),
    (HtmlConnettore, dict(
        id="dait", nome="Min. Interno - DAIT Finanza locale", livello="nazionale",
        ente="Ministero dell'Interno - DAIT",
        lista=[f"https://dait.interno.gov.it/finanza-locale/notizie?page={p}" for p in range(0, 3)],
        sel_item=".views-row", sel_titolo="h2 a",
        dettaglio=True, sel_corpo=".field--name-body", max_dettagli=40,
        titolo_da_corpo=True)),

    # --- API strutturate -----------------------------------------------------
    (PloneConnettore, dict(
        id="rer-bandi", attiva=False, nome="Regione Emilia-Romagna", livello="regionale", regione="Emilia-Romagna",
        base="https://www.regione.emilia-romagna.it/leggi-atti-bandi/bandi-finanziamenti-contributi", bando_certo=True, scadenza_esposta=True)),
    (SocrataConnettore, dict(
        id="lom-bandi", attiva=False, nome="Regione Lombardia - Bandi Online", livello="regionale", regione="Lombardia",
        dominio="https://www.dati.lombardia.it", dataset="bukx-h2uy",
        url_tpl="https://www.bandi.regione.lombardia.it/servizi/servizio/agevolazioni/{codice}",
        map=dict(titolo="titolo_bando", codice="codice_bando", ente="ente",
                 direzione="direzione_generale", apertura="apertura_adesione",
                 scadenza="chiusura_adesione", tema="tipo_strumento", ordine="codice_bando"), bando_certo=True, scadenza_esposta=True)),
    (SediaConnettore, dict(bando_certo=True, scadenza_esposta=True)),
    (PugliaConnettore, dict(bando_certo=True, scadenza_esposta=True)),
    (PaDigitaleConnettore, dict(bando_certo=True, scadenza_esposta=True)),
    # La Serie Generale pubblica di tutto: niente bando_certo, filtra il classificatore.
    (RssConnettore, dict(
        id="gazzetta-sg", nome="Gazzetta Ufficiale - Serie Generale", livello="nazionale",
        feed="https://www.gazzettaufficiale.it/rss/SG",
        ente="Gazzetta Ufficiale della Repubblica Italiana")),

    # --- RSS -----------------------------------------------------------------
    (RssConnettore, dict(
        id="pie-bandi", attiva=False, nome="Regione Piemonte - Bandi", livello="regionale", regione="Piemonte",
        feed="https://bandi.regione.piemonte.it/tutti/rss.xml", max=400, bando_certo=True)),
    (RssConnettore, dict(
        id="cal-europa", attiva=False, nome="Calabria Europa", livello="regionale", regione="Calabria",
        feed="https://calabriaeuropa.regione.calabria.it/feed/", pagine=5)),
    (RssConnettore, dict(
        id="sic-euroinfo", attiva=False, nome="EuroInfoSicilia", livello="regionale", regione="Sicilia",
        feed="https://www.euroinfosicilia.it/feed/", pagine=5)),
    (RssConnettore, dict(
        id="fvg-news", attiva=False, nome="Regione Friuli-Venezia Giulia", livello="regionale", regione="Friuli-VG",
        feed="http://www.regione.fvg.it/rafvg/cms/RAFVG/rss/notizie-in-evidenza")),
]

CKAN_DA_ESPLORARE = [
    ("dati.gov.it", "https://www.dati.gov.it/opendata"),
    ("Toscana", "https://dati.toscana.it"),
    ("Liguria", "https://dati.regione.liguria.it"),
    ("Trento", "https://dati.trentino.it"),
    ("Bolzano", "https://data.civis.bz.it"),
]


def esegui(solo: str | None = None):
    http = Http(delay=1.5)
    con = connect()
    tot_n = tot_nuovi = 0
    print(f"{'FONTE':<34} {'ESITO':<8} {'TROV':>6} {'NUOVI':>6} {'COMUNI+':>8}  NOTE")
    print("-" * 92)
    for cls, cfg in FONTI:
        c = cls(http, **cfg)
        if cfg.get("attiva") is False and not solo:
            continue
        if solo and c.id != solo:
            continue
        try:
            bandi = c.fetch()
            n, nuovi = salva(con, bandi)
            rilevanti = sum(1 for b in bandi if b.score_comuni >= 2.0)
            log_ingest(con, c.id, "ok", n, nuovi)
            tot_n += n
            tot_nuovi += nuovi
            print(f"{c.nome[:33]:<34} {'ok':<8} {n:>6} {nuovi:>6} {rilevanti:>8}")
        except Exception as e:
            log_ingest(con, c.id, "errore", 0, 0, f"{type(e).__name__}: {e}")
            print(f"{c.nome[:33]:<34} {'ERRORE':<8} {0:>6} {0:>6} {0:>8}  {type(e).__name__}: {str(e)[:45]}")
            if "-v" in sys.argv:
                traceback.print_exc()
    print("-" * 92)
    print(f"{'TOTALE':<34} {'':<8} {tot_n:>6} {tot_nuovi:>6}")
    return con


def esplora_ckan():
    http = Http(delay=1.5)
    print("\nCKAN discovery - dataset candidati con risorse strutturate")
    print("-" * 92)
    for nome, base in CKAN_DA_ESPLORARE:
        d = CkanDiscovery(http, id=f"ckan-{nome}", nome=nome, base=base)
        try:
            res = d.scopri()
            if not res:
                print(f"{nome:<14} nessun dataset candidato")
            for r in res[:4]:
                fmts = ",".join(sorted({x['fmt'] for x in r['risorse']}))
                print(f"{nome:<14} [{fmts:<8}] {r['dataset'][:60]}")
        except Exception as e:
            print(f"{nome:<14} ERRORE {type(e).__name__}")


if __name__ == "__main__":
    con = esegui()
    if "--ckan" in sys.argv:
        esplora_ckan()
