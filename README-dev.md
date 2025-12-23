# DDMX - Guide Developpeur

## Architecture rapide
- Backend : Flask (`app.py`) gere l'API, rend `templates/index.html`.
- Moteur DMX : `dmx_engine.py` (thread 40 Hz, verrous RLock) fusionne valeurs directes, effets, fades, identify, et envoie ArtNet via `DMXE.py`.
- Effets : `Effect.py` calcule les offsets [-1,1] a partir de `effects_definitions.json`.
- Frontend : JS/CSS dans `static/` (`core.js`, `rig.js`, `cues.js`, `effects.js`, `ui.js`, `popup.js`, `i18n.js`, langues dans `static/lang/`).

## Lancer en dev
1) `pip install flask` (et dependances futures si ajoutees).
2) `python app.py` (debug=True par defaut). UI sur http://localhost:5000.
3) ArtNet : cible par defaut `127.0.0.1`, `bind_ip="0.0.0.0"` dans `init_engine()` ; ajustez selon votre reseau.

## Points de configuration
- Reseau ArtNet : modifiez `DMXRenderEngine(artnet_ip=..., bind_ip=...)` dans `app.py`.
- Port HTTP : changez `app.run(..., port=5000)` si besoin.
- Dossiers : `fixtures/` et `cue/` sont crees au demarrage ; gardez-les versionnes.

## Routes cles (app.py)
- Fixtures : `GET /api/fixtures` (parsing XML via `parse_fixture_xml`).
- Cues : `GET /api/cue_files`, `GET/POST /api/cues/<filename>`, `POST /api/playback/go|stop`.
- Live : `POST /api/live/channels`, `POST /api/live/effect/start|stop`, `POST /api/identify/start|stop`, `GET /api/state/stream` (SSE), `POST /api/apply_state` (legacy).
- Utilitaires : `GET /api/effects`, `GET /static/<path>`.

## Reperes code
- `dmx_engine.py` : logique de rendu, gestion des univers, identify overlay, callbacks SSE via `add_state_callback`.
- `DMXE.py` : envoi ArtNet bas niveau (packets, scheduling).
- `Effect.py` : helpers de parsing (phase, amplitude, frequence) et evaluation d'effets.
- Front : `core.js` stocke l'etat global (rig, cues, selection), `rig.js` gere le layout des devices, `cues.js` l'editeur de sequence, `effects.js` l'UI d'effets, `i18n.js` charge `static/lang/*.json`.

## Tests & qualite
- Pas de tests automat ises presents. A ajouter : parsing de fixtures/cues, unite sur `dmx_engine.py` (fades, overlay identify), validation des effets.
- Garder les fichiers UTF-8, eviter les regressions sur les routes publiques et le flux SSE.

## Contribution et extensions
- Nouveaux effets : ajoutez la definition dans `effects_definitions.json` et l'implementation dans `Effect.py` si nouveau type.
- Nouvelles fixtures/cues : placez les fichiers dans `fixtures/` et `cue/` (l'API et l'UI les listent automatiquement).
- Internationalisation : synchronisez les cles `data-i18n` dans `templates/index.html` avec les JSON de `static/lang/`.
- Performance : surveillez la charge CPU du thread 40 Hz et les verrous lors d'ajouts de calculs.
