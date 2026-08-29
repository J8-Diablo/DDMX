# DDMX 0.4.0 — Spécification de release

> Release groupée (« tout d'un bloc ») : **Notion de Projet** + **correction des problèmes élevés/moyens**.
>
> L'IA AutoLight, qui formait la priorité 2 de cette spec, a été retirée du produit : ses chapitres ont été supprimés de ce document.
> Document de référence issu de l'interview de cadrage. Source de vérité pour l'implémentation.

---

## 0. Portée et séquencement

- **Une seule release** contenant les trois chantiers.
- Ordre de priorité de réalisation : **1) Projet → 2) Refonte IA → 3) Fixes élevés/moyens**, puis validation et release.
- **Validation** : jeu de morceaux de référence (EDM, DnB, house, jazz/soft…) avec vérité terrain mesurable (BPM/mesures/sections détectés vs attendus) **ET** validation live.
- Version cible : **0.4.0** (release majeure).

---

## 1. PRIORITÉ 1 — Notion de Projet

### 1.1 Définition
Un **Projet = un Rig + ses cue lists associées**. Les réglages IA restent **globaux à l'application** (pas embarqués dans le projet).

Le Rig comprend : les devices (fixtures, adresses, univers, positions X/Y) **+ la calibration des positions** (voir §3.5 : position « public » + flags d'inversion pan/tilt par fixture).

### 1.2 Menu « Projet » dans la top bar
Entrées : **Charger · Récent · Sauvegarder · Sauvegarder sous · Importer · Exporter**.

### 1.3 Format
- **Fichier unique portable** (`.ddmxproj`) : facile à partager/importer/exporter.

### 1.4 Comportement de reset (le bug à corriger)
- Charger un projet, ou ouvrir un **nouveau projet vierge**, **réinitialise proprement le Rig et le compteur d'ID** (nouveau projet vierge ⇒ on repart à l'**ID 1**).
- **Découpler le Rig des cue lists** : changer de cue list *à l'intérieur d'un projet* ne doit **pas** redéfinir le Rig.

### 1.5 Bug de rémanence (confirmé dans le code)
- `register_rig_devices()` (`dmx_engine.py` ~l.1983) **n'efface jamais** les devices orphelins ⇒ devices fantômes + canaux résiduels au changement de cue.
- **À faire** : endpoint backend de **reset du Rig** (`_devices`, `_live_effects`, `_cue_effects` vidés + canaux remis à zéro) appelé au chargement de projet ; resync propre ensuite.
- Compteur d'ID `nextDeviceId` (`core.js:11`, `rig.js`) à réinitialiser côté front **et** backend.

---

## 4. Problèmes élevés/moyens à corriger dans cette release

> Issus de l'analyse de fond. À traiter dans le chantier « fixes ».

### Élevés
- **Concurrence** : `SETTINGS` (dict global) et champs comme `_playback_wait_adjust_ms` mutés sans verrou alors que le thread de rendu les lit → verrou / snapshot immuable.
- **SSE** : `_sse_clients.append()/remove()` non atomiques pendant l'itération de broadcast.
- **Fuites mémoire** : `_chaser_random_seeds` (`Effect.py`), cache `music_sources._cache`, `_last_sent_universes` (`dmx_engine.py`) jamais purgés.

### Moyens
- **Parsing de phase dupliqué et divergent** : `Effect.py` (secondes) vs `intelligent_fx.py` (ms) → unifier (une fonction, **en ms**).
- **Doublons JS** : `toast`, `confirmModal/alertModal/promptModal`, helpers `t()/tfmt()` redéfinis (core.js/ui.js/sync_video.js) → consolider.
- **i18n incomplet** : 6 langues à 84 % (de, es, it, nl, pt, ge), 56 clés manquantes ; doublon `ge`/`de`.
- **`pytest` absent de `requirements.txt`** alors que les tests en dépendent.
- **Code mort JS** : `initSplitLayout()`, bouton `pause-cues`, `_effectMemberIds`, etc.

---

## 5. Récapitulatif des décisions (table)

| Sujet | Décision |
|---|---|
| Mouvement repos | Retour vers public, **position « public » réglable + invert par fixture** |
| Taille parc | Jusqu'à 129+ fixtures, multi-univers |
| Calibration | Panneau dédié (pan/tilt public + invert flags) |
| Contenu projet | Rig + cue lists |
| Format projet | Fichier unique portable `.ddmxproj` |
