# DDMX 0.4.0 — Spécification de release

> Release groupée (« tout d'un bloc ») : **Notion de Projet** + **Refonte complète de l'IA AutoLight** + **correction des problèmes élevés/moyens**.
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

## 2. PRIORITÉ 2 — Refonte complète de l'IA AutoLight

> Décision : **refonte complète** (nouvelle architecture analyse + décision + effets). Le cœur du problème actuel : détection par seuils absolus, pas de phase-lock, comptage de mesures naïf, zéro anticipation.

### 2.1 Cœur d'analyse musicale (à réécrire)
- **Ancrage du beat** : **kick + snare (backbeat)** — grosse caisse (temps 1/3) + caisse claire (temps 2/4) pour retrouver la grille même quand le kick manque.
- **Beat-grid avec phase-lock** : non seulement *détecter* les kicks mais connaître leur **position dans la mesure** (LA pièce manquante aujourd'hui).
- **Verrouillage du tempo : très rapide (<2 s)**, avec **auto-correction visible** si erreur.
- **Demi/double-tempo** : choisir le **tempo « dancefloor » ressenti**, biaisé vers la plage **128-155**. Le **BPM officiel de la DB tranche** l'ambiguïté s'il est connu.
- **Autorité BPM** : **DB d'abord** (lookup titre/artiste), **audio en secours** ; **tap tempo écrase tout** ; la **phase** vient toujours de l'audio.
- **Métrique** : **majoritairement 4/4**, tolérer 3/4·6/8 occasionnels (ne pas casser).
- **Comptage hiérarchique** : beats → **mesures (4)** → **phrases (longueur auto-détectée 8/16/32)** → sections. Comptage **resynchronisé** (pas un `+1` naïf), **remis à zéro au seek / changement de morceau**.
- **Détection de section** : fenêtres plus rapides qu'actuellement ; **ne plus exiger `bass_dominant`** pour reconnaître un drop (capter aussi les montées synthé/pad).
- **Transitions de set DJ** : détecter blancs/transitions/changement brutal de track, faire une **respiration/fondu propre**, puis **re-verrouiller** le tempo du nouveau morceau (pas de strobe orphelin ni coupure brutale).

### 2.2 Métadonnées en ligne
- Récupérer : **genre/mood, BPM officiel, clé musicale, structure/sections** (si dispo).
- **Plusieurs bases publiques en cascade (fallback)** — à évaluer : MusicBrainz/AcousticBrainz, Deezer, GetSongBPM ; réutiliser le client SoundCloud existant. *(Faisabilité technique à confirmer ; pas de clé payante.)*
- **Hors-ligne / morceau inconnu** : bascule **transparente sur détection audio pure**, **sans dégradation visible** ni attente réseau.

### 2.3 Dramaturgie / décision
- **Contraste énergétique très marqué** (calmes très calmes, drops qui explosent).
- **Breakdowns** : **réduire fortement** (ambiance douce, peu/pas de flash) pour construire la tension avant le drop.
- **Anticipation** : **dès que la phrase est calée**, préparer le drop dans les **2-4 dernières mesures** d'une montée (monter, resserrer, puis lâcher au temps 1).
- **Biais énergique** mais **récupération immédiate et gracieuse** en cas de faux déclenchement.
- **Timing des transitions visuelles** : calé sur les **frontières de phrase**, **+ changement immédiat sur un drop détecté** (même hors frontière).
- **Downbeat** : marquage **subtil / au feeling**, mais **accent fort sur le « 1 » de phrase** (toutes les 16 mesures ≈ le drop).

### 2.4 Vocabulaire visuel par section
*(Principe directeur transversal : **rester agréable, ne jamais en faire trop**.)*
- **Calme** : fondus de couleur lents, mouvements amples et lents (**ou pas de mouvement**), respiration ponctuelle ; **peut utiliser tout le parc ou seulement quelques fixtures**.
- **Montée** : accélération progressive, montée d'intensité globale, strobe croissant, convergence/resserrement — **toutes valides, dosées**.
- **Drop/Peak** : impact franc puis **groove calé sur le kick**, strobe + couleurs saturées, mouvements rapides/chases, **adapté au genre** — **dosé**.
- **Strobe** : usage **libre** ; l'IA peut décider d'utiliser **uniquement les fixtures de type strobe**.
  - ⚠️ **Canal Focus des strobes** : 0 = faisceau resserré (éclaire devant), 255 = éclaire tout le parc. L'IA **pilote le focus** et l'**ouvre progressivement sur la montée**.

### 2.5 Couleur
- **Source** : **dérivée du mood/genre de la DB**.
- **Cadence** : **lente au calme**, **franche aux drops/transitions de phrase**.
- **Pic** : dominante **selon le genre**.
- **Harmonie** : **auto selon l'intensité** (mono/analogue au calme → complémentaire/contrasté aux peaks).

### 2.6 Utilisation du Rig
- **Rôles fixtures** : **hybride** (rôle de base par type, sur-classement ponctuel sur les moments forts).
- **Géométrie** : **symétrie miroir G/D**, **chases/vagues à travers le parc**, **clusters/zones** (essentiel à grande échelle).
- **Sous-ensembles actifs** : **selon l'énergie** (peu de fixtures au calme, tout le parc au drop — le nombre de fixtures actives devient un paramètre d'intensité).
- **Mouvement (pan/tilt) au repos** : les têtes **reviennent vers le public**, avec une **position « public » réglable par fixture** + **flags d'inversion pan/tilt** (certaines fixtures sont orientées différemment ou montées à l'envers).
- **Échelle cible** : **jusqu'à 129+ fixtures sur plusieurs univers** ⇒ rendu **performant**, clusters/zones indispensables.

### 2.7 Bibliothèque d'effets
- **Nouvelle bibliothèque** pensée pour le DJ-engine (effets calés mesure/phrase) **+ conserver les meilleurs** effets intelligents/legacy existants.

### 2.8 Mémoire / apprentissage
- **Mémoriser la structure** d'un morceau réentendu (pour mieux anticiper) **+ les préférences/satisfaction** utilisateur. Améliorer la base de mémoire existante.

---

## 3. Contrôle, UX et garde-fous

### 3.1 Modes
- **Plein-auto** ET **assist**, **commutables en live**.

### 3.2 Override manuel
- **Lâcher seulement ce que je touche** : l'IA rend la main sur le device/groupe manipulé, **continue à piloter le reste**, et **reprend ce device après un délai d'inactivité**.

### 3.3 Transparence (interface)
- **Vue DJ complète** : BPM + confiance, **compteur temps/mesure/phrase**, section courante, intent, **drop anticipé**, **beat-grid visuelle**. Permet de comprendre et corriger les erreurs.

### 3.4 Calage temporel
- **Adaptatif** : serré/punchy sur drops/peaks, fluide/adouci dans les calmes.

### 3.5 Garde-fous (« rester agréable », adaptable au lieu)
- **Plafond d'intensité global** réglable.
- **Mode « petit lieu »** (préréglage sobre).
- **Mini-panel UI de correction en live** pour ajuster ces garde-fous en cas de problème.

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
- **Pipeline AutoLight « effects » hérité** : devient caduc avec la refonte → retirer proprement.
- **Code mort JS** : `initSplitLayout()`, bouton `pause-cues`, `_effectMemberIds`, etc.

---

## 5. Récapitulatif des décisions (table)

| Sujet | Décision |
|---|---|
| Genres prio | EDM d'abord, doux/jazz géré (mode soft **auto via genre détecté**) |
| BPM typique | 128-155 |
| Source audio | Mix loopback / tracks / micro |
| Métrique | Majoritairement 4/4 |
| Ancrage beat | Kick + snare (backbeat) |
| Lock tempo | Très rapide (<2 s), auto-correction |
| Demi/double | Dancefloor ressenti ; DB tranche |
| Autorité BPM | DB d'abord, audio secours, tap écrase |
| Phrase | Longueur **auto-détectée** |
| Anticipation | Oui, dès phrase calée |
| Downbeat | Subtil ; accent fort sur le « 1 » de phrase |
| Transitions visuelles | Frontières de phrase + drops immédiats |
| Contraste | Très contrasté |
| Breakdown | Réduire fort |
| Incertitude | Énergique + récupération immédiate |
| Transitions set | Détecter et gérer proprement |
| FX calme | Fondus lents, mouvements amples/ou pas, respiration, parc partiel |
| FX montée | Toutes (dosées) |
| FX drop | Toutes, adaptées au genre (dosées) |
| Strobe | Libre ; pilote le **Focus** (ouvre sur la montée) |
| Rôles fixtures | Hybride |
| Géométrie | Miroir G/D + chases/vagues + clusters/zones |
| Sous-ensembles | Selon l'énergie |
| Couleur source | Mood DB |
| Couleur cadence | Lente calme / franche drops |
| Couleur pic | Selon genre |
| Harmonie | Auto selon intensité |
| Métadonnées | Genre/mood + BPM + clé + structure |
| Source DB | Plusieurs en cascade |
| Mémoire | Structure + préférences |
| Repli DB | Audio pur, sans accroc |
| Modes IA | Auto + assist, commutables live |
| Override | Lâcher seulement ce qu'on touche |
| Transparence | Vue DJ complète |
| Synchro | Adaptative |
| Garde-fous | Plafond intensité + mode petit lieu + mini-panel UI |
| Mouvement repos | Retour vers public, **position « public » réglable + invert par fixture** |
| Taille parc | Jusqu'à 129+ fixtures, multi-univers |
| Bibliothèque FX | Nouvelle + garder les meilleurs |
| Calibration | Panneau dédié (pan/tilt public + invert flags) |
| Contenu projet | Rig + cue lists (IA reste globale) |
| Format projet | Fichier unique portable `.ddmxproj` |
