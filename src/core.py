"""Nucleo di BandiPA: modello, storage, HTTP client, classificazione."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse

import requests
from dateutil import parser as dtparser

DB_PATH = Path(__file__).parent / "bandi.db"
UA = "BandiPA/0.2 (aggregatore istituzionale bandi per Comuni; contatto: admin@example.it)"

# --------------------------------------------------------------------------- HTTP


class Http:
    """Sessione con rate limit per host, retry e cache condizionale."""

    def __init__(self, delay: float = 2.0, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
        self.delay = delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.cache: dict[str, dict] = {}

    def _wait(self, url: str):
        host = urlparse(url).netloc
        with self._lock:
            prev = self._last.get(host, 0)
            gap = time.time() - prev
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last[host] = time.time()

    def request(self, method: str, url: str, retries: int = 4, **kw):
        headers = dict(kw.pop("headers", {}))
        tmo = kw.pop("timeout_override", None) or self.timeout
        ent = self.cache.get(url)
        if ent and method == "GET":
            if ent.get("etag"):
                headers["If-None-Match"] = ent["etag"]
            if ent.get("last_modified"):
                headers["If-Modified-Since"] = ent["last_modified"]
        last_exc = None
        for attempt in range(retries):
            self._wait(url)
            try:
                r = self.s.request(method, url, headers=headers, timeout=tmo,
                                   verify=False, allow_redirects=True, **kw)
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(2 ** attempt * 2)
                    continue
                if method == "GET" and r.status_code == 200:
                    self.cache[url] = {"etag": r.headers.get("ETag"),
                                       "last_modified": r.headers.get("Last-Modified")}
                return r
            except requests.RequestException as e:
                last_exc = e
                time.sleep(2 ** attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"richieste esaurite: {url}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# --------------------------------------------------------------------------- Modello


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_data(v) -> str | None:
    """Restituisce ISO date da formati eterogenei (IT/EN/ISO)."""
    if not v:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s or s.lower() in ("none", "null", "-"):
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        try:
            return date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            return None
    try:
        return dtparser.parse(s, dayfirst=True, fuzzy=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_importo(v) -> float | None:
    if v is None:
        return None
    s = str(v)
    m = re.search(r"(\d[\d.\s]*(?:,\d+)?)", s.replace("\u00a0", " "))
    if not m:
        return None
    n = m.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        val = float(n)
    except ValueError:
        return None
    low = s.lower()
    if "milion" in low or re.search(r"\bmln\b", low):
        val *= 1_000_000
    elif "miliard" in low or re.search(r"\bmld\b", low):
        val *= 1_000_000_000
    return val


@dataclass
class Bando:
    titolo: str
    url: str
    fonte_id: str
    fonte_nome: str
    livello: str                 # UE | nazionale | regionale
    regione: str | None = None
    ente: str | None = None
    descrizione: str = ""
    data_pubblicazione: str | None = None
    data_apertura: str | None = None
    data_scadenza: str | None = None
    dotazione: float | None = None
    beneficiari: list[str] = field(default_factory=list)
    tema: str | None = None
    score_comuni: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        chiave = f"{slugify(self.titolo)[:110]}|{slugify(self.ente or self.fonte_nome)[:40]}"
        return hashlib.sha1(chiave.encode()).hexdigest()[:16]

    @property
    def stato(self) -> str:
        oggi = date.today().isoformat()
        if self.data_scadenza and self.data_scadenza < oggi:
            return "chiuso"
        if self.data_apertura and self.data_apertura > oggi:
            return "programmato"
        if self.data_scadenza:
            giorni = (dtparser.parse(self.data_scadenza).date() - date.today()).days
            return "in scadenza" if giorni <= 15 else "aperto"
        return "aperto" if (self.data_apertura or self.data_pubblicazione) else "sconosciuto"


# --------------------------------------------------------------------------- Classificazione

# Peso positivo: il Comune e' potenziale beneficiario
POSITIVI = {
    r"\bcomun[ie]\b": 3.0,
    r"\benti local[ie]\b": 3.0,
    r"\bunion[ie] di comuni\b": 3.0,
    r"\bcitt[aà] metropolitan": 1.0,
    r"\bprovinc[ei]\b": 0.8,
    r"\benti pubblic[ie]\b": 1.5,
    r"\bamministrazion[ie] (?:pubblich[ei]|local[ie]|comunal[ie])\b": 2.0,
    r"\bpubblich[ei] amministrazion[ie]\b": 1.5,
    r"\bsoggett[oi] pubblic[oi]\b": 1.0,
    r"\baree interne\b": 1.0,
    r"\bpubblica amministrazione\b": 1.5,
    r"\blocal (?:authorit|government)": 2.5,
    r"\bmunicipalit(?:y|ies)\b": 3.0,
}
# Peso negativo: platea chiaramente non comunale
NEGATIVI = {
    r"\bmicro[, ]?piccol[ei] e medi[ei] impres[ei]\b": -2.0,
    r"\bpm[ei]\b": -1.5,
    r"\bimpres[ei]\b": -1.0,
    r"\blibere? profession": -1.5,
    r"\bpersone fisiche\b": -1.5,
    r"\bcittadin[ie]\b": -1.0,
    r"\bstudent[ie]\b": -1.5,
    r"\bassociazion[ie] sportiv": -1.5,
    r"\bagricoltor[ie]\b": -1.5,
    r"\bconcorso pubblico\b": -3.0,
    r"\bselezione per (?:titoli|esami)\b": -3.0,
    r"\bassunzione\b": -2.0,
    r"\bmanifestazione di interesse per l['e ]affidamento\b": -2.0,
    r"\bgara d['e ]appalto\b": -3.0,
    r"\bprocedura aperta per\b": -2.0,
}

TEMI = {
    "digitalizzazione": r"digital|cloud|pnrr m1|cybersic|informatic|pagopa|spid|anncsu|banda larga",
    "ambiente e energia": r"ambient|energetic|rinnovabil|fotovoltaic|efficientamento|rifiut|idric|forestaz",
    "mobilità": r"mobilit|ciclabil|trasport|piste ciclab|tpl|autobus",
    "edilizia e rigenerazione": r"rigenerazione urban|edilizi|riqualificazione|ristrutturazion|patrimonio immobiliar|scolastic",
    "sociale e welfare": r"social|welfare|povert|disabil|anzian|infanzia|asili|violenza|inclusion",
    "cultura e turismo": r"cultura|museo|bibliotec|turis|borghi|beni cultural|teatr",
    "sicurezza": r"videosorveglianz|sicurezza urbana|protezione civile|sismic|dissesto",
    "sport": r"sportiv|impiantistic|palestr|piscin",
    "scuola e formazione": r"scuol|formazion|istruzion|didattic|educativ",
}


MARCATORI_DEST = re.compile(
    r"(destinatar|beneficiar|possono (?:presentare|partecipare|candidars|accedere)|"
    r"rivolt[oa] a|riservat[oa] a|soggetti ammissibili|chi pu[oò] partecipare|"
    r"eligible (?:applicants|entities)|who can apply)", re.I)


def _finestre_destinatari(testo: str) -> str:
    """Estrae i frammenti che seguono un marcatore di platea: li' i match valgono di piu'."""
    out = []
    for m in MARCATORI_DEST.finditer(testo):
        out.append(testo[m.end(): m.end() + 260])
    return " ".join(out)


def classifica(b: Bando) -> Bando:
    testo = " ".join(filter(None, [b.titolo, b.descrizione, " ".join(b.beneficiari)])).lower()
    zona = _finestre_destinatari(testo)
    campo_dest = " ".join(b.beneficiari).lower()
    score = 0.0
    trovati = []
    for pat, peso in POSITIVI.items():
        if re.search(pat, testo):
            # il match vale di piu' se compare nel campo destinatari o subito dopo un marcatore
            moltiplicatore = 1.0
            if campo_dest and re.search(pat, campo_dest):
                moltiplicatore = 1.8
            elif zona and re.search(pat, zona):
                moltiplicatore = 1.5
            score += peso * moltiplicatore
            trovati.append(re.sub(r"[\\\b()?:|\[\]+]", "", pat).strip())
    for pat, peso in NEGATIVI.items():
        if re.search(pat, testo):
            score += peso
    b.score_comuni = round(score, 2)
    if not b.beneficiari and trovati:
        b.beneficiari = sorted(set(trovati))[:5]
    if not b.tema:
        for tema, pat in TEMI.items():
            if re.search(pat, testo):
                b.tema = tema
                break
    return b


# --------------------------------------------------------------------------- Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS bandi (
  uid TEXT PRIMARY KEY,
  titolo TEXT NOT NULL, url TEXT, fonte_id TEXT, fonte_nome TEXT,
  livello TEXT, regione TEXT, ente TEXT, descrizione TEXT,
  data_pubblicazione TEXT, data_apertura TEXT, data_scadenza TEXT,
  dotazione REAL, beneficiari TEXT, tema TEXT, score_comuni REAL,
  stato TEXT, first_seen TEXT, last_seen TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_scad ON bandi(data_scadenza);
CREATE INDEX IF NOT EXISTS ix_liv ON bandi(livello, regione);
CREATE INDEX IF NOT EXISTS ix_score ON bandi(score_comuni);
CREATE VIRTUAL TABLE IF NOT EXISTS bandi_fts USING fts5(
  titolo, descrizione, ente, content='bandi', content_rowid='rowid', tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS ingest_log (
  ts TEXT, fonte_id TEXT, esito TEXT, n_trovati INTEGER, n_nuovi INTEGER, messaggio TEXT
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def salva(con: sqlite3.Connection, bandi: list[Bando]) -> tuple[int, int]:
    ora = datetime.now().isoformat(timespec="seconds")
    nuovi = 0
    for b in bandi:
        cur = con.execute("SELECT uid FROM bandi WHERE uid=?", (b.uid,))
        esiste = cur.fetchone() is not None
        if not esiste:
            nuovi += 1
        con.execute("""
            INSERT INTO bandi (uid,titolo,url,fonte_id,fonte_nome,livello,regione,ente,descrizione,
              data_pubblicazione,data_apertura,data_scadenza,dotazione,beneficiari,tema,score_comuni,
              stato,first_seen,last_seen,raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
              titolo=excluded.titolo, url=excluded.url, descrizione=excluded.descrizione,
              data_scadenza=excluded.data_scadenza, data_apertura=excluded.data_apertura,
              dotazione=COALESCE(excluded.dotazione, bandi.dotazione),
              tema=COALESCE(excluded.tema, bandi.tema), score_comuni=excluded.score_comuni,
              stato=excluded.stato, last_seen=excluded.last_seen
        """, (b.uid, b.titolo, b.url, b.fonte_id, b.fonte_nome, b.livello, b.regione, b.ente,
              b.descrizione[:4000], b.data_pubblicazione, b.data_apertura, b.data_scadenza,
              b.dotazione, json.dumps(b.beneficiari, ensure_ascii=False), b.tema, b.score_comuni,
              b.stato, ora, ora, json.dumps(b.raw, ensure_ascii=False, default=str)[:20000]))
    con.execute("INSERT INTO bandi_fts(bandi_fts) VALUES('rebuild')")
    con.commit()
    return len(bandi), nuovi


def log_ingest(con, fonte_id, esito, n, nuovi, msg=""):
    con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), fonte_id, esito, n, nuovi, msg[:500]))
    con.commit()
