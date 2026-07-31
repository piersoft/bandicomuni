"""Connettore per fonti HTML senza API. Configurabile via selettori CSS.

Progettato per due modalita':
  - solo lista: ogni scheda contiene gia' titolo, link e testo utile (es. Coesione)
  - lista + dettaglio: la lista ha solo titoli generici e il contenuto sta nella
    pagina interna (es. i comunicati DAIT, intitolati solo con la data)
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from connectors import Connettore
from core import parse_data, parse_importo

MESI = ("gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        "settembre|ottobre|novembre|dicembre")
RE_DATA = re.compile(rf"\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{4}}|\d{{1,2}}\s+(?:{MESI})\s+\d{{4}}", re.I)
RE_SCADENZA = re.compile(
    rf"(?:scadenz\w*|entro(?:\s+il)?|termine\w*\s+(?:di\s+presentazione|ultimo)?|"
    rf"fino\s+al|al)\s*[:\s]\s*(\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{4}}|\d{{1,2}}\s+(?:{MESI})\s+\d{{4}})",
    re.I)
RE_IMPORTO = re.compile(
    r"(?:euro|€|eur)\s*([\d.]+(?:,\d+)?)\s*(milion\w*|miliard\w*)?|"
    r"([\d.]+(?:,\d+)?)\s*(?:milion\w*|mln)\s*(?:di\s*)?(?:euro|€)", re.I)


def _testo(nodo) -> str:
    return re.sub(r"\s+", " ", nodo.text(separator=" ", strip=True)) if nodo else ""


class HtmlConnettore(Connettore):
    """cfg attesi:
        lista        URL (o lista di URL) delle pagine indice
        sel_item     selettore delle schede
        sel_titolo   selettore del titolo dentro la scheda (default: primo <a>)
        sel_link     selettore del link (default: primo <a>)
        dettaglio    bool, se scaricare la pagina interna
        sel_corpo    selettore del corpo nella pagina di dettaglio
        max_dettagli quante pagine interne scaricare al massimo
        titolo_da_corpo  se True, il titolo della lista e' inutile e va arricchito
    """

    def fetch(self):
        pagine = self.cfg["lista"]
        if isinstance(pagine, str):
            pagine = [pagine]
        grezzi = []
        for url in pagine:
            r = self.http.get(url)
            if r.status_code != 200:
                if url == pagine[0]:
                    r.raise_for_status()
                continue
            grezzi.extend(self._estrai_lista(r.text, url))

        # deduplica per URL mantenendo l'ordine
        visti, items = set(), []
        for it in grezzi:
            if it["url"] and it["url"] not in visti:
                visti.add(it["url"])
                items.append(it)

        if self.cfg.get("dettaglio"):
            for it in items[: self.cfg.get("max_dettagli", 25)]:
                self._arricchisci(it)

        out = []
        for it in items:
            if not it["titolo"]:
                continue
            testo = f"{it['titolo']} {it['testo']}"
            m = RE_SCADENZA.search(testo)
            out.append(self._b(
                titolo=it["titolo"][:400],
                url=it["url"],
                ente=self.cfg.get("ente", self.nome),
                descrizione=it["testo"][:1500],
                data_pubblicazione=it.get("data_pub"),
                data_scadenza=parse_data(m.group(1)) if m else None,
                dotazione=self._importo(testo),
                raw={"fonte_lista": it.get("origine")},
            ))
        return out

    # ---------------------------------------------------------------- interni

    def _estrai_lista(self, html: str, base: str) -> list[dict]:
        t = HTMLParser(html)
        fuori = []
        for nodo in t.css(self.cfg["sel_item"]):
            a = (nodo.css_first(self.cfg["sel_link"]) if self.cfg.get("sel_link")
                 else nodo.css_first("a"))
            if a is None:
                continue
            href = a.attributes.get("href") or ""
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            tn = (nodo.css_first(self.cfg["sel_titolo"]) if self.cfg.get("sel_titolo") else a)
            testo = _testo(nodo)
            date = RE_DATA.findall(testo)
            fuori.append({
                "titolo": _testo(tn) or _testo(a),
                "url": urljoin(base, href),
                "testo": testo,
                "data_pub": parse_data(date[0]) if date else None,
                "origine": base,
            })
        return fuori

    def _arricchisci(self, it: dict):
        try:
            r = self.http.get(it["url"])
            if r.status_code != 200:
                return
        except Exception:
            return
        t = HTMLParser(r.text)
        corpo = None
        for sel in (self.cfg.get("sel_corpo"), ".field--name-body", "article", "main"):
            if sel:
                corpo = t.css_first(sel)
                if corpo:
                    break
        testo = _testo(corpo)
        if not testo:
            return
        it["testo"] = f"{it['testo']} {testo}"[:4000]
        # Titoli come "Comunicato del 27 luglio 2026" non dicono nulla:
        # li si arricchisce con la prima frase informativa del corpo.
        if self.cfg.get("titolo_da_corpo"):
            frase = self._frase_utile(testo)
            if frase:
                it["titolo"] = f"{it['titolo']} - {frase}"

    @staticmethod
    def _frase_utile(testo: str) -> str:
        testo = re.sub(r"^\s*(si comunica che|si informa che|si rende noto che)\s*", "",
                       testo, flags=re.I)
        # scarta gli incipit che non descrivono l'oggetto (es. "Aggiornamento del ...")
        rumore = re.compile(r"^(aggiornament|si (?:comunica|informa|rende)|"
                            r"nel sito|e' disponibile|si pubblica|con riferimento)", re.I)
        frasi = [f.strip() for f in re.split(r"(?<=[.;])\s+", testo) if len(f.strip()) > 40]
        for f in frasi:
            if not rumore.match(f):
                return f[:180]
        if frasi:
            return frasi[0][:180]
        return testo[:180].strip()

    @staticmethod
    def _importo(testo: str) -> float | None:
        m = RE_IMPORTO.search(testo)
        if not m:
            return None
        if m.group(1):
            return parse_importo(f"{m.group(1)} {m.group(2) or ''}")
        return parse_importo(f"{m.group(3)} milioni")
