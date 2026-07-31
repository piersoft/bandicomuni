#!/usr/bin/env python3
"""Sonda i portali regionali/nazionali per capire quale pattern di accesso supportano.
Pattern testati: plone.restapi (++api++), CKAN, Socrata (SODA), RSS."""
import json
import re
import warnings
from concurrent.futures import ThreadPoolExecutor

import requests

warnings.filterwarnings("ignore")
UA = {"User-Agent": "Mozilla/5.0 (compatible; BandiPA-probe/0.1)"}
T = 30

CANDIDATI = {
    "Abruzzo": ["https://www.regione.abruzzo.it/content/bandi", "https://opendata.regione.abruzzo.it"],
    "Basilicata": ["https://www.regione.basilicata.it/giunta/site/giunta/department.jsp?dep=100435", "https://dati.regione.basilicata.it"],
    "Calabria": ["https://calabriaeuropa.regione.calabria.it/bandi", "https://www.regione.calabria.it"],
    "Campania": ["https://www.regione.campania.it/regione/it/tematiche/bandi-e-avvisi", "https://dati.regione.campania.it"],
    "Emilia-Romagna": ["https://www.regione.emilia-romagna.it/leggi-atti-bandi/bandi-finanziamenti-contributi", "https://dati.emilia-romagna.it"],
    "Friuli-VG": ["https://bandi.regione.fvg.it", "https://www.regione.fvg.it", "https://www.dati.friuliveneziagiulia.it"],
    "Lazio": ["https://www.regione.lazio.it/enti/bandi", "https://dati.lazio.it"],
    "Liguria": ["https://www.regione.liguria.it/homepage/bandi.html", "https://dati.regione.liguria.it"],
    "Lombardia": ["https://www.bandi.regione.lombardia.it", "https://www.dati.lombardia.it"],
    "Marche": ["https://www.regione.marche.it/Entra-in-Regione/Bandi", "https://dati.regione.marche.it"],
    "Molise": ["https://www.regione.molise.it", "https://dati.regione.molise.it"],
    "Piemonte": ["https://bandi.regione.piemonte.it", "https://www.dati.piemonte.it"],
    "Puglia": ["https://www.regione.puglia.it/web/bandi-e-avvisi", "https://www.sistema.puglia.it", "https://dati.puglia.it"],
    "Sardegna": ["https://www.regione.sardegna.it/servizi/cittadino/contributi", "https://dati.regione.sardegna.it"],
    "Sicilia": ["https://www.regione.sicilia.it", "https://dati.regione.sicilia.it", "https://www.euroinfosicilia.it"],
    "Toscana": ["https://www.regione.toscana.it/bandi", "https://dati.toscana.it"],
    "Trento": ["https://www.provincia.tn.it", "https://dati.trentino.it"],
    "Bolzano": ["https://www.provincia.bz.it", "https://data.civis.bz.it"],
    "Umbria": ["https://www.regione.umbria.it/bandi", "https://dati.umbria.it"],
    "Valle d'Aosta": ["https://www.regione.vda.it", "https://opendata.regione.vda.it"],
    "Veneto": ["https://bandi.regione.veneto.it", "https://dati.veneto.it"],
    "NAZ-dati.gov.it": ["https://www.dati.gov.it/opendata"],
    "NAZ-coesione": ["https://politichecoesione.governo.it/it/finanziamenti-avvisi-e-bandi"],
    "NAZ-italiadomani": ["https://www.italiadomani.gov.it/it/opportunita/bandi-amministrazioni-titolari.html"],
    "NAZ-padigitale": ["https://padigitale2026.gov.it"],
    "NAZ-dait": ["https://dait.interno.gov.it/finanza-locale"],
}


def get(url, hdr=None, **kw):
    for _ in range(2):
        try:
            return requests.get(url, headers=hdr or UA, timeout=T, verify=False, allow_redirects=True, **kw)
        except requests.exceptions.Timeout:
            continue
    raise requests.exceptions.Timeout(url)


def try_plone(base):
    for path in (f"{base.rstrip('/')}/++api++/@search?portal_type=Bando&b_size=1",
                 f"{base.rstrip('/')}/++api++/@search?b_size=1"):
        try:
            r = get(path, hdr={**UA, "Accept": "application/json"})
            if r.status_code == 200 and "items_total" in r.text[:4000]:
                d = r.json()
                return {"n": d.get("items_total"), "url": path}
        except Exception:
            pass
    return None


def try_ckan(base):
    try:
        r = get(f"{base.rstrip('/')}/api/3/action/package_search?q=bandi&rows=1")
        if r.status_code == 200 and r.json().get("success"):
            return {"n": r.json()["result"].get("count"), "url": f"{base.rstrip('/')}/api/3/action/"}
    except Exception:
        pass
    return None


def try_socrata(base):
    try:
        r = get(f"{base.rstrip('/')}/api/catalog/v1?q=bandi&limit=1")
        if r.status_code == 200 and "resultSetSize" in r.text[:3000]:
            return {"n": r.json().get("resultSetSize"), "url": f"{base.rstrip('/')}/api/catalog/v1"}
    except Exception:
        pass
    return None


def try_rss(base):
    try:
        r = get(base)
        if r.status_code != 200:
            return None
        m = re.findall(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', r.text, re.I)
        hrefs = []
        for tag in m:
            h = re.search(r'href=["\']([^"\']+)["\']', tag)
            if h:
                hrefs.append(h.group(1))
        if hrefs:
            return {"n": len(hrefs), "url": hrefs[0][:110]}
    except Exception:
        pass
    return None


def probe(item):
    regione, urls = item
    out = {"regione": regione, "plone": None, "ckan": None, "socrata": None, "rss": None, "http": {}}
    for u in urls:
        try:
            out["http"][u] = get(u).status_code
        except Exception as e:
            out["http"][u] = type(e).__name__
        for name, fn in (("plone", try_plone), ("ckan", try_ckan), ("socrata", try_socrata), ("rss", try_rss)):
            if out[name] is None:
                res = fn(u)
                if res:
                    out[name] = res
    return out


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(probe, CANDIDATI.items()))
    json.dump(results, open("probe_results.json", "w"), ensure_ascii=False, indent=1)
    print(f"{'REGIONE':<18} {'PLONE':<10} {'CKAN':<10} {'SOCRATA':<10} {'RSS':<6} HTTP")
    print("-" * 88)
    for r in sorted(results, key=lambda x: x["regione"]):
        f = lambda k: (str(r[k]["n"])[:9] if r[k] else "-")
        codes = ",".join(str(v) for v in r["http"].values())
        print(f"{r['regione']:<18} {f('plone'):<10} {f('ckan'):<10} {f('socrata'):<10} {f('rss'):<6} {codes}")
