"""Connettori per le fonti istituzionali."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from datetime import date

import feedparser

from core import Bando, Http, classifica, parse_data, parse_importo


class Connettore:
    id = "base"
    nome = "base"
    livello = "nazionale"
    regione: str | None = None

    def __init__(self, http: Http, **cfg):
        self.http = http
        self.cfg = cfg
        for k in ("id", "nome", "livello", "regione"):
            if k in cfg:
                setattr(self, k, cfg[k])

    def fetch(self) -> list[Bando]:
        raise NotImplementedError

    def _b(self, **kw) -> Bando:
        kw.setdefault("fonte_id", self.id)
        kw.setdefault("fonte_nome", self.nome)
        kw.setdefault("livello", self.livello)
        kw.setdefault("regione", self.regione)
        b = classifica(Bando(**kw))
        # Alcune fonti sono cataloghi di soli bandi (Plone portal_type=Bando,
        # anagrafica Socrata, calls SEDIA): li' l'essere un bando e' garantito
        # dalla struttura della fonte, non va cercato nel testo del titolo.
        if self.cfg.get("bando_certo"):
            b.score_bando = round(b.score_bando + 2.5, 2)

        # Distingue l'assenza reale dal nostro limite di estrazione.
        # Se la fonte espone un campo scadenza dedicato e lo lascia vuoto,
        # il bando quel termine non ce l'ha (tipicamente: procedura a sportello).
        if b.data_scadenza:
            b.scadenza_stato = "data"
        elif self.cfg.get("scadenza_esposta"):
            b.scadenza_stato = "assente"
        else:
            b.scadenza_stato = "ignota"
        return b


# --------------------------------------------------------------------------- Plone


class PloneConnettore(Connettore):
    """Portali Plone / Design Italia con plone.restapi (++api++)."""

    def fetch(self) -> list[Bando]:
        base = self.cfg["base"].rstrip("/")
        out, start, size = [], 0, 50
        while True:
            url = (f"{base}/++api++/@search?portal_type=Bando&metadata_fields=_all"
                   f"&sort_on=effective&sort_order=descending&b_size={size}&b_start={start}")
            r = self.http.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            d = r.json()
            items = d.get("items", [])
            for it in items:
                ente = it.get("ente_bando")
                if isinstance(ente, list):
                    ente = ", ".join(str(x) for x in ente) or None
                dest = it.get("destinatari_bando") or []
                if isinstance(dest, str):
                    dest = [dest]
                out.append(self._b(
                    titolo=(it.get("title") or "").strip(),
                    url=it.get("@id") or it.get("getURL") or "",
                    ente=ente or self.nome,
                    descrizione=(it.get("description") or "").strip(),
                    data_pubblicazione=parse_data(it.get("effective")),
                    data_apertura=parse_data(it.get("apertura_bando")),
                    data_scadenza=parse_data(it.get("scadenza_bando") or it.get("expires")),
                    beneficiari=[str(x) for x in dest],
                    tema=it.get("tipologia_bando"),
                    raw={k: it.get(k) for k in ("bando_state", "tipologia_bando",
                                                "tassonomia_argomenti", "UID") if it.get(k)},
                ))
            start += size
            if len(items) < size or start >= d.get("items_total", 0) or start > 2000:
                break
        return out


# --------------------------------------------------------------------------- Socrata


class SocrataConnettore(Connettore):
    """Dataset Socrata (SODA) con mappatura campi configurabile."""

    def fetch(self) -> list[Bando]:
        url = f"{self.cfg['dominio'].rstrip('/')}/resource/{self.cfg['dataset']}.json"
        m = self.cfg["map"]
        out, offset, limit = [], 0, 400
        while True:
            r = self.http.get(url, timeout_override=90, params={"$limit": limit, "$offset": offset,
                                           "$order": m.get("ordine", ":id")})
            r.raise_for_status()
            righe = r.json()
            for x in righe:
                tit = (x.get(m["titolo"]) or "").strip()
                if not tit:
                    continue
                cod = x.get(m.get("codice", ""), "")
                out.append(self._b(
                    titolo=tit,
                    url=self.cfg.get("url_tpl", "").format(codice=cod) if cod else self.cfg.get("url_base", ""),
                    ente=x.get(m.get("ente", ""), self.nome),
                    descrizione=x.get(m.get("descrizione", ""), "") or x.get(m.get("direzione", ""), ""),
                    data_apertura=parse_data(x.get(m.get("apertura", ""))),
                    data_scadenza=parse_data(x.get(m.get("scadenza", ""))),
                    dotazione=parse_importo(x.get(m.get("dotazione", ""))),
                    tema=x.get(m.get("tema", "")) or None,
                    raw=x,
                ))
            offset += limit
            if len(righe) < limit or offset > 20000:
                break

        # Alcune anagrafiche regionali elencano anche finanziamenti a regia
        # (interventi diretti su un singolo ente, ordinanze, ribassi d'asta):
        # hanno tipo_strumento "Bando" ma nessuna finestra di adesione, perche'
        # una candidatura non esiste. Restano in archivio, non fra i bandi aperti.
        if self.cfg.get("richiede_finestra"):
            for b in out:
                if not b.data_scadenza:
                    b.score_bando = round(b.score_bando - 5.0, 2)
                    b.scadenza_stato = "ignota"
        return out


# --------------------------------------------------------------------------- SEDIA (UE)


class SediaConnettore(Connettore):
    """EU Funding & Tenders Portal - search API SEDIA."""

    id = "ue-sedia"
    nome = "EU Funding & Tenders Portal"
    livello = "UE"
    ENDPOINT = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    STATUS_APERTI = ["31094501", "31094502"]  # forthcoming, open

    def fetch(self) -> list[Bando]:
        query = {"bool": {"must": [
            {"terms": {"type": ["1", "2"]}},
            {"terms": {"status": self.STATUS_APERTI}},
            {"terms": {"language": ["en"]}},
        ]}}
        out, page, size = [], 1, 100
        visti = set()
        while page <= 12:
            r = self.http.post(
                f"{self.ENDPOINT}?apiKey=SEDIA&text=***&pageSize={size}&pageNumber={page}",
                files={"query": (None, json.dumps(query), "application/json")})
            r.raise_for_status()
            d = r.json()
            res = d.get("results", [])
            for x in res:
                md = x.get("metadata", {})
                g = lambda k: (md.get(k) or [None])[0] if isinstance(md.get(k), list) else md.get(k)
                ident = g("identifier") or g("callIdentifier") or x.get("reference")
                if not ident or ident in visti:
                    continue
                visti.add(ident)
                titolo = (x.get("summary") or g("callTitle") or ident or "").strip()
                out.append(self._b(
                    titolo=titolo[:400],
                    url=x.get("url", ""),
                    ente=g("frameworkProgramme") or "Commissione europea",
                    descrizione=re.sub(r"<[^>]+>", " ", (x.get("content") or ""))[:1500].strip(),
                    data_scadenza=parse_data(g("deadlineDate")),
                    data_pubblicazione=parse_data(g("startDate") or g("es_SortDate")),
                    dotazione=parse_importo(g("budgetOverview")),
                    tema=g("programmeDivision"),
                    raw={"identifier": ident, "callIdentifier": g("callIdentifier"),
                         "programmePeriod": g("programmePeriod"), "status": g("sortStatus")},
                ))
            if len(res) < size:
                break
            page += 1
        return out


# --------------------------------------------------------------------------- RSS


class RssConnettore(Connettore):
    """Feed RSS/Atom generico. Il rumore viene filtrato a valle dallo score."""

    def fetch(self) -> list[Bando]:
        entries, pagine = [], self.cfg.get("pagine", 1)
        for p in range(1, pagine + 1):
            url = self.cfg["feed"] if p == 1 else f"{self.cfg['feed']}?paged={p}"
            r = self.http.get(url)
            if r.status_code != 200:
                if p == 1:
                    r.raise_for_status()
                break
            e = feedparser.parse(r.content).entries
            if not e:
                break
            entries.extend(e)
        out = []
        visti = set()
        for e in entries[: self.cfg.get("max", 300)]:
            if e.get("link") in visti:
                continue
            visti.add(e.get("link"))
            titolo = re.sub(r"<[^>]+>", " ", e.get("title", "")).strip()
            if not titolo:
                continue
            desc = re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:1200].strip()
            out.append(self._b(
                titolo=titolo,
                url=e.get("link", ""),
                ente=self.cfg.get("ente", self.nome),
                descrizione=desc,
                data_pubblicazione=parse_data(e.get("published") or e.get("updated")),
                data_scadenza=self._scadenza(f"{titolo} {desc}"),
                raw={"tags": [t.get("term") for t in e.get("tags", [])][:5]},
            ))
        return out

    @staticmethod
    def _scadenza(testo: str) -> str | None:
        m = re.search(r"scadenz\w*[:\s]*(?:il\s+)?(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})", testo, re.I)
        if m:
            return parse_data(m.group(1))
        m = re.search(r"entro (?:il\s+)?(\d{1,2}\s+\w+\s+\d{4})", testo, re.I)
        return parse_data(m.group(1)) if m else None


# --------------------------------------------------------------------------- CKAN discovery


class CkanDiscovery(Connettore):
    """Non produce bandi: cerca dataset candidati e ne elenca le risorse."""

    def fetch(self) -> list[Bando]:
        return []

    def scopri(self) -> list[dict]:
        base = self.cfg["base"].rstrip("/")
        trovati = []
        for q in ("bandi", "avvisi", "contributi", "incentivi", "finanziamenti"):
            try:
                r = self.http.get(f"{base}/api/3/action/package_search",
                                  params={"q": q, "rows": 10})
                if r.status_code != 200:
                    continue
                for p in r.json().get("result", {}).get("results", []):
                    ris = [{"fmt": (x.get("format") or "").upper(), "url": x.get("url")}
                           for x in p.get("resources", [])
                           if (x.get("format") or "").upper() in ("CSV", "JSON", "XML")]
                    if ris:
                        trovati.append({"portale": self.nome, "dataset": p.get("title"),
                                        "name": p.get("name"), "risorse": ris[:3]})
            except Exception:
                continue
        # dedup per name
        uniq = {t["name"]: t for t in trovati}
        return list(uniq.values())


# --------------------------------------------------------------------------- Puglia


class PugliaConnettore(Connettore):
    """Catalogo bandi di Regione Puglia (Liferay + React).

    Il portlet espone un export CSV, ma e' piu' povero della pagina: niente URL della
    scheda, righe vuote e intestazione duplicata. Le card HTML contengono invece
    titolo, link, descrizione, area tematica e sempre entrambe le date.
    """

    id = "pug-catalogo"
    nome = "Regione Puglia - Catalogo bandi"
    livello = "regionale"
    regione = "Puglia"
    PORTLET = "it_servizidigitali_catalogobandi_CatalogoBandiFrontendPortlet"
    BASE = "https://sistema.regione.puglia.it/catalogo-bandi"
    # Il portlet filtra la platea alla fonte: CITTADINI | IMPRESE | ENTI_OPERATORI.
    # Interrogare ENTI_OPERATORI e' piu' affidabile che dedurre i beneficiari dal
    # testo: molte schede indicano come "destinatari" i beneficiari finali
    # (es. i migranti) mentre a candidarsi sono i Comuni.
    URL = f"{BASE}?p_p_id={PORTLET}&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view" \
          f"&_{PORTLET}_filterBeneficiari=ENTI_OPERATORI"

    def fetch(self) -> list[Bando]:
        from selectolax.parser import HTMLParser

        r = self.http.get(self.URL)
        r.raise_for_status()
        doc = HTMLParser(r.text)
        pulisci = lambda n: re.sub(r"\s+", " ", n.text(strip=True)) if n else ""

        out = []
        for a in doc.css('a[href*="scheda-bando"]'):
            card = a
            for _ in range(5):
                if card.parent:
                    card = card.parent
                if "card-body" in str(card.attributes.get("class", "")):
                    break

            titolo = pulisci(card.css_first(".card-title"))
            if not titolo:
                continue
            # separator: senza, i nodi adiacenti si incollano ("ApertoSRG01 - ...")
            blob = re.sub(r"\s+", " ", card.text(separator=" ", strip=True))
            estrai = lambda p: (re.search(p, blob).group(1) if re.search(p, blob) else None)

            # lo stato sta in un nodo dedicato: leggerlo dal blob lo incollerebbe al titolo
            stato = pulisci(card.css_first(".category-top")).replace("Stato:", "").strip()
            sottotitoli = [pulisci(n) for n in card.css(".card-subtitle")]
            tipologia = next((s for s in sottotitoli if "Data " not in s), None)

            out.append(self._b(
                titolo=titolo,
                url=a.attributes.get("href", self.BASE),
                ente="Regione Puglia",
                # dichiarazione della fonte, non deduzione nostra
                beneficiari=["Enti e Operatori"],
                descrizione=pulisci(card.css_first(".card-text")),
                # il portale localizza le date secondo Accept-Language:
                # gg/mm/aaaa in italiano, ISO altrimenti. Accetta entrambi.
                data_apertura=parse_data(estrai(r"Data apertura:\s*([\d/.-]{8,10})")),
                data_scadenza=parse_data(estrai(r"Data chiusura:\s*([\d/.-]{8,10})")),
                tema=tipologia,
                raw={"stato_fonte": stato, "filtro_fonte": "ENTI_OPERATORI"},
            ))

        # La fonte dichiara che questi bandi sono per enti e non per cittadini o
        # imprese: e' un segnale strutturale, vale piu' di una parola nel titolo.
        for b in out:
            b.score_comuni = round(b.score_comuni + 2.0, 2)
        return out


# --------------------------------------------------------------------------- PA digitale 2026


class PaDigitaleConnettore(Connettore):
    """Avvisi di PA digitale 2026 (Dipartimento per la trasformazione digitale).

    Il portale e' una SPA Next.js, ma il Team Digitale pubblica l'anagrafica degli
    avvisi come open data versionati su GitHub. Unica fonte finora con un campo
    'soggetti_destinatari' esplicito: la platea non va dedotta dal testo.
    """

    id = "padigitale2026"
    nome = "PA digitale 2026"
    livello = "nazionale"
    URL = ("https://raw.githubusercontent.com/teamdigitale/"
           "padigitale2026-opendata/main/data/avvisi.json")
    PAGINA = "https://padigitale2026.gov.it/avvisi"

    def fetch(self) -> list[Bando]:
        r = self.http.get(self.URL)
        r.raise_for_status()
        out = []
        for x in r.json():
            titolo = (x.get("titolo") or "").strip()
            if not titolo:
                continue
            dest = x.get("soggetti_destinatari") or ""
            out.append(self._b(
                titolo=titolo,
                url=self.PAGINA,
                ente="Dipartimento per la trasformazione digitale",
                descrizione=x.get("misura") or "",
                data_apertura=parse_data(x.get("data_inizio_bando")),
                data_scadenza=parse_data(x.get("data_fine_bando")),
                dotazione=x.get("totale_importo_stanziato"),
                beneficiari=[d.strip() for d in re.split(r"[,;/]", dest) if d.strip()],
                tema="digitalizzazione",
                raw={"misura": x.get("misura"), "stato_fonte": x.get("stato"),
                     "totale_importo_misura": x.get("totale_importo_misura")},
            ))
        return out


# --------------------------------------------------------------------------- Gazzetta Ufficiale


class GazzettaConnettore(Connettore):
    """Serie Generale della Gazzetta Ufficiale, ultimi 30 giorni.

    Il feed RSS espone solo l'ultimo numero: cinque atti al giorno, e nulla del
    pregresso. La pagina /30giorni elenca invece tutti i numeri dell'ultimo mese;
    da ognuno si legge il sommario e se ne estraggono gli atti. E' la sola fonte
    in cui converge ogni decreto di contributo agli enti locali, qualunque sia il
    ministero che lo adotta.
    """

    id = "gazzetta-sg"
    nome = "Gazzetta Ufficiale - Serie Generale"
    livello = "nazionale"
    BASE = "https://www.gazzettaufficiale.it"

    # La Serie Generale pubblica ~550 atti al mese, quasi tutti amministrativi.
    # Senza un filtro dedicato l'archivio si riempirebbe di aggiornamenti di
    # pericolosita' idraulica e proroghe di stato di emergenza. Si tengono solo
    # gli atti che aprono o disciplinano una candidatura.
    APRE_CANDIDATURA = re.compile(
        r"avviso pubblico|bando|manifestazione di interesse|"
        r"present\w+ (?:delle |di )?(?:domand|istanz|richiest|proposte)|"
        r"modalit\w+ (?:di |per )?(?:presentazione|richiesta|accesso)|"
        r"termin\w+ per la presentazione|candidatur|"
        r"selezione (?:di|dei|delle) (?:progett|propost|intervent)", re.I)
    ESCLUDI = re.compile(
        r"aggiornamento della pericolosit|stato di emergenza|"
        r"revoca|graduatoria|nomina|scioglimento|"
        r"riconoscimento della personalit|cambiamento di denominazione|"
        r"variazione del|liquidazione coatta|regolamento recante", re.I)
    ELENCO = f"{BASE}/30giorni/serie_generale"
    ARCHIVIO = f"{BASE}/ricercaArchivioCompleto/serie_generale/{{anno}}?anno={{anno}}"
    ENTE = "Gazzetta Ufficiale della Repubblica Italiana"

    def _numeri(self) -> list[tuple[str, str]]:
        from selectolax.parser import HTMLParser

        # Di norma gli ultimi 30 giorni. Con anno=YYYY si legge l'archivio
        # annuale completo (175 numeri per il 2026), per recuperare il pregresso.
        anno = self.cfg.get("anno")
        r = self.http.get(self.ARCHIVIO.format(anno=anno) if anno else self.ELENCO)
        r.raise_for_status()
        visti, out = set(), []
        for a in HTMLParser(r.text).css("a"):
            h = a.attributes.get("href") or ""
            if "caricaDettaglio" not in h:
                continue
            data = re.search(r"dataPubblicazioneGazzetta=([\d-]+)", h)
            num = re.search(r"numeroGazzetta=(\d+)", h)
            if data and num and (data.group(1), num.group(1)) not in visti:
                visti.add((data.group(1), num.group(1)))
                out.append((data.group(1), num.group(1)))
        return out

    def fetch(self) -> list[Bando]:
        from selectolax.parser import HTMLParser

        out = []
        for data, num in self._numeri()[: self.cfg.get("max_numeri", 30)]:
            url = (f"{self.BASE}/gazzetta/serie_generale/caricaDettaglio"
                   f"?dataPubblicazioneGazzetta={data}&numeroGazzetta={num}")
            try:
                r = self.http.get(url)
                if r.status_code != 200:
                    continue
            except Exception:
                continue
            doc = HTMLParser(r.text)
            for sp in doc.css("span.risultato"):
                a = sp.css_first('a[href*="caricaDettaglioAtto"]')
                if a is None:
                    continue
                intero = re.sub(r"\s+", " ", sp.text(separator=" ", strip=True))
                intestazione = re.sub(r"\s+", " ", a.text(strip=True))
                oggetto = intero.replace(intestazione, "", 1).strip(" .")
                if not oggetto:
                    oggetto = intestazione
                link = a.attributes.get("href", "")
                if self.ESCLUDI.search(oggetto) or not self.APRE_CANDIDATURA.search(
                        f"{oggetto} {intestazione}"):
                    continue
                out.append(self._b(
                    titolo=oggetto[:400],
                    url=link if link.startswith("http") else self.BASE + link,
                    ente=self.ENTE,
                    descrizione=f"{intestazione} — Serie Generale n. {num} del {data}",
                    data_pubblicazione=parse_data(data),
                    raw={"numero_gazzetta": num, "atto": intestazione[:200]},
                ))

        # Il sommario non riporta i termini e il testo integrale e' solo in PDF:
        # per gli atti della GU la scadenza resta ignota. Un decreto pubblicato
        # da oltre tre mesi con termine sconosciuto e' quasi certamente scaduto:
        # resta consultabile in archivio ma non viene proposto come aperto.
        limite = (date.today() - timedelta(days=self.cfg.get("giorni_validita", 90))).isoformat()
        for b in out:
            if not b.data_scadenza and (b.data_pubblicazione or "") < limite:
                b.score_bando = round(b.score_bando - 5.0, 2)
        return out
