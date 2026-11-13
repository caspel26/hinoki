# Handoff — Aptum

## START HERE

Stato al 13 novembre 2026: specifica 0.2, fonti delle regole raccolte e contratto base di sintassi confermato; implementazione non avviata. Nome definitivo: **Aptum**, con `aptum` previsto per import e CLI.

Leggere `START_HERE.md`, `FRAMEWORK_SPEC.md`, `RUNTIME_AND_MODULES.md`, `RULES_PROVENANCE.md` e `rules/AGENTS.md`. Quest'ultimo è una bozza per il futuro repository; gli originali GitHub sono in `sources/`.

Prima di modificare la superficie pubblica leggere `SYNTAX_CONTRACT.md`. Decisione del proprietario: Python semplice, `@get/@post/...`, policy `Access` riutilizzabili e opzioni `access`/`transaction` sul decoratore; funzioni e classi con più metodi. Scartati i blocchi `with get/post` e la proposta di endpoint inline con `query/lookup`. La sintassi dei CRUD generati resta aperta.

Posizionamento concordato: architettura verificabile con regole facoltative, contesto AI/MCP reale, personalizzazione coerente e moduli espliciti. Il lavoro successivo nella conversazione è il branding/logo di Aptum; la prima implementazione tecnica resta Q001.

## Decisioni del proprietario da preservare

Python tipizzato e semplice; core Rust per richieste e ORM; indipendenza da altri framework; schemi/settings propri senza Pydantic; funzioni e API class con più metodi; ORM sul modello; docs automatiche/custom; CRUD e regole facoltativi; auth pubblica di default e provider estendibili; errori/paginazione/relazioni; admin extra, più temi e override HTML.

Preservare anche il requisito di servizi modulari/microservizi, con app espandibili a moduli, lifespan, thread/processi e avvio espliciti. Granian è il server proposto dal proprietario, non una dipendenza già installata. Raccomandazione del documento: prototipo ASGI proprio su Granian e confronto RSGI prima della scelta definitiva.

## Open work

| ID | Lavoro | Dipendenze | Uscita |
| --- | --- | --- | --- |
| Q001 | Design runtime, moduli e bridge | Nessuna | ADR con ownership delle risorse, confini Granian/Rust/Python e prototipo minimo pianificato |
| Q002 | Prova di typing e sintassi | Q001 per integrazione, design indipendente | Firme schema/ORM/decoratori verificabili da type checker e IDE |
| Q003 | Bootstrap repository e gate | Q001 | Struttura, toolchain, test e comandi reali documentati |
| Q004 | Vertical slice blog | Q001–Q003 | Richiesta reale, schema, ORM, auth, rollback, docs e paginazione |
| Q005 | Relazioni e CRUD generato | Q004 | Contratti coerenti e accesso agli oggetti verificato |
| Q006 | Regole e development MCP | Primo nucleo già in Q004 | Diagnostica deterministica e tool strutturati |
| Q007 | Admin extra | Q004–Q006 | Un tema e override HTML dimostrati, migrazioni opt-in |

Nessun item è dichiarato completato. Il pacchetto documentale non conta come implementazione di Q001.

## Brief iniziale da dare a un agente

> Progetta Q001 senza avviare la costruzione dell'intero framework. Leggi specifica, runtime/moduli e regole consolidate. Scrivi `docs/RUNTIME-DESIGN.md` e le ADR necessarie. Confronta il nostro adattatore ASGI su Granian con RSGI; identifica esattamente quali percorsi sono Rust e dove attraversiamo Python. Definisci factory, grafo dei moduli, startup fallito, cleanup inverso, loop/pool per worker, executor sync limitato, contesto transazionale, cancellazioni e shutdown. Se il backend richiede un secondo runtime Rust per ORM, esplicita costo e ownership. Elenca verifiche di SIGTERM, isolamento a due worker, startup failure e sync che non blocca async. Proponi un default e passi implementativi piccoli con file ownership. Non introdurre Django, FastAPI, Starlette o Pydantic; non creare admin o backend multipli. Distingui risultati misurati da ipotesi e API proposte. Registra decisioni aperte e aggiorna questo handoff.

## Verifica effettivamente disponibile

In questo pacchetto non ci sono server, pacchetti installabili, migrazioni o script di test del framework. `examples/blog.py` è un contratto illustrativo: si può verificarne la sintassi Python, non importarlo ed eseguirlo come prodotto esistente.

Q003 deve fissare entry point reali per lint/format, type check, Python/Rust unit e integration test, coverage, DB usa e getta, OpenAPI, wheel e benchmark. Non dichiarare passati comandi ancora da creare.

## Migrazioni e deploy

Nessuna migrazione applicata. Nessun repository remoto modificato, push o deploy eseguito. Il modello account dell'admin non esiste ancora; installare l'extra futuro non dovrà mutare il DB.
