# BandiPA

Aggregatore di bandi e avvisi pubblici a cui i **Comuni italiani** possono candidarsi come
beneficiari. Raccoglie da fonti istituzionali UE, nazionali e regionali, normalizza i dati,
scarta gli avvisi non rivolti agli enti locali e pubblica il risultato come sito statico.

## Architettura

```
GitHub Actions (cron 04:17 UTC)
  └─ src/registry.py   connettori -> SQLite (artefatto di build, in cache tra i run)
       └─ src/export.py  -> docs/data/*.json
            └─ commit su main -> GitHub Pages
                 └─ docs/index.html  filtri e ricerca lato client
```

Nessun server, nessun database di runtime, nessun costo di esercizio.

## Fonti attive

| Fonte | Livello | Pattern |
|---|---|---|
| Dip. Politiche di Coesione | nazionale | HTML |
| Min. Interno - DAIT Finanza locale | nazionale | HTML lista + dettaglio |
| Regione Emilia-Romagna | regionale | Plone REST API (`++api++`) |
| Regione Lombardia - Bandi Online | regionale | Socrata SODA (`bukx-h2uy`) |
| EU Funding & Tenders Portal | UE | search-api SEDIA |
| Regione Piemonte | regionale | RSS |
| Calabria Europa, EuroInfoSicilia, Regione FVG | regionale | RSS |

`src/probe_sources.py` sonda i portali per scoprire quale pattern supportano: da rieseguire
prima di aggiungere una fonte nuova.

## Fonti nazionali non accessibili

| Fonte | Ostacolo |
|---|---|
| Italia Domani | 403 anche con User-Agent browser: protezione anti-bot |
| PA digitale 2026 | contenuto caricato via JavaScript, nessuna API pubblica trovata |
| IFEL / pnrrcomuni | 503: protezione anti-bot |
| Gazzetta Ufficiale | gli endpoint RSS restituiscono HTML, non feed validi |

Richiedono un browser headless (Playwright) nel workflow.

## Criterio di rilevanza

La selezione usa **due assi indipendenti**.

`score_comuni` - il Comune e' un possibile beneficiario. Ogni bando riceve uno `score_comuni` da un classificatore a regole. I termini pesano di piu'
se compaiono nel campo destinatari (x1.8) o subito dopo marcatori come "possono presentare
domanda" (x1.5), cosi' che l'ente *erogatore* non venga scambiato per il *beneficiario*.
`score_bando` - e' un'opportunita' ancora aperta e non un atto gia' concluso. Serve perche'
molte fonti istituzionali pubblicano soprattutto riparti, trasferimenti e graduatorie: citano
i Comuni, ma non c'e' nulla da presentare. Le fonti che sono cataloghi di soli bandi
(`bando_certo=True`) ricevono un bonus strutturale: li' l'informazione sta nella natura della
fonte, non nel testo.

Soglia di pubblicazione: `score_comuni >= 2.0` **e** `score_bando >= 0`. La seconda condizione
scarta l'evidenza negativa ma ammette i casi ambigui: uno zero significa "nessun indizio",
non "non e' un bando".

## Uso locale

```bash
pip install -r requirements.txt
cd src && python registry.py && python export.py
python -m http.server -d ../docs 8000
```

## Dati generati

| File | Contenuto |
|---|---|
| `docs/data/bandi.json` | perimetro utile: aperti e rilevanti per i Comuni |
| `docs/data/archivio.json` | chiusi o sotto soglia |
| `docs/data/meta.json` | statistiche, faccette, esito ultimo run per fonte |
| `docs/data/last_run.json` | timestamp; garantisce un commit giornaliero |

## Limiti noti

- Alcuni portali regionali applicano protezioni anti-bot: da runner GitHub possono
  rispondere 503. Il workflow non fallisce, ma la fonte va monitorata in `meta.json`.
- Il canale UE e' poco selettivo: il testo SEDIA non contiene la sezione eligibility,
  quindi il filtro per parole chiave non discrimina. Va sostituito da un filtro per programma.
- Il dataset Socrata della Lombardia non espone i destinatari: la classificazione lavora
  solo sul titolo, con conseguente sottostima.
