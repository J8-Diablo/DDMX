# DummyDMX - Guide Utilisateur

## Vue d'ensemble
DummyDMX est un controleur DMX/ArtNet avec interface web (Flask) pour piloter vos projecteurs, gerer des cues JSON et appliquer des effets en temps reel.
<img width="1845" height="752" alt="Capture d&#39;écran 2025-12-24 003907" src="https://github.com/user-attachments/assets/34c1bf47-48f7-4efc-ab61-ac8e5a556257" />
<img width="1381" height="436" alt="Capture d&#39;écran 2025-12-24 003923" src="https://github.com/user-attachments/assets/368a2d39-5a8e-468e-902a-ac5074d7f62a" />
<img width="1827" height="615" alt="Capture d&#39;écran 2025-12-24 003935" src="https://github.com/user-attachments/assets/c5aab0fe-f1f9-4130-aa4b-81b6331b5313" />



## Pre-requis
- Python 3.10+ installe
- Reseau permettant l'envoi ArtNet (cible par defaut 127.0.0.1)
- Fixtures XML dans `fixtures/`
- (Optionnel) Cues JSON dans `cue/`

## Installation rapide
1) Creez/activez un venv si besoin.
2) Installez Flask : `pip install flask` (ou `pip install -r requirements.txt` si present).
3) Verifiez vos fixtures dans `fixtures/` et vos cues dans `cue/` (les dossiers sont crees automatiquement).

## Lancement du serveur
- Demarrer : `python app.py`
- Acces UI : http://localhost:5000
- ArtNet : la cible est `127.0.0.1` (changeable dans `app.py` via `DMXRenderEngine(artnet_ip=...)`).

## Utilisation de l'interface
- Barre superieure : choix de langue (EN/FR), creation de nouveau JSON, Play/Pause/Skip/Stop, Stop FX, Identify ON/OFF.
- Onglet Rig : import des fixtures, ajout de devices, adressage DMX, selection multiple, groupes virtuels.
- Onglet Cues : editeur sequentiel (boucle, sequences), lecture via Play, Skip avance d'une etape, Stop arrete lecture/effets.
- Onglet Effects : applique/retire des effets live sur les devices selectionnes ; "Stop FX" coupe les effets actifs.
- Identify : met en surbrillance les devices selectionnes (via `/api/identify/start|stop`).

## Gestion des fichiers
- Fixtures : placez vos XML dans `fixtures/` (parser decrit dans `app.py` > `parse_fixture_xml`).
- Cues : JSON dans `cue/` (structure avec `loop`, `virtual_groups`, `sequence`, etc.). L'UI propose "New JSON" pour un modele vierge.
- Effets : catalogue dans `effects_definitions.json` (charge par `/api/effects`).

## API principales
- `GET /api/fixtures` : liste les fixtures parses.
- `GET /api/cue_files` : liste des cues disponibles.
- `GET/POST /api/cues/<filename>` : lire/ecrire une cue JSON.
- `POST /api/live/channels` : ecrire des valeurs DMX `{universe, channels:{ch:val}}`.
- `POST /api/live/effect/start|stop` : demarrer/arret er un effet live sur un device.
- `POST /api/playback/go|stop` : lancer/arret er la lecture d'une cue.
- `POST /api/identify/start|stop` : bascule du mode Identify.
- `GET /api/state/stream` : flux SSE de l'etat live.
- `GET /api/effects` : liste des effets disponibles.

## Depannage rapide
- Pas de sortie ArtNet : verifiez l'IP cible dans `app.py` et la connectivite reseau.
- Aucun fixture visible : XML mal forme ou manquant dans `fixtures/`.
- UI vide ou boutons inactifs : controlez la console du navigateur pour les appels API (port/CORS).
- Effets inactifs : assurez-vous que les devices sont selectionnes et que Stop FX n'est pas actif.

## Aller plus loin
- Ajoutez de nouveaux effets dans `effects_definitions.json` et, si necessaire, le calcul associe dans `Effect.py`.
- Dupliquez/editez des cues dans `cue/` pour creer des shows personnalises.
- Changez l'IP ArtNet ou le port Flask selon votre reseau.
