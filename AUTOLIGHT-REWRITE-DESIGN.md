# AutoLight 2.0 — Conception technique de la refonte IA

> Complément technique de `RELEASE-0.4.0-SPEC.md` (P2). Décrit l'architecture du
> nouveau moteur « DJ » : beat-grid à phase-lock, comptage mesures/phrases,
> anticipation, et comment il se branche dans l'existant.

## Principe directeur

**Garder la plomberie, remplacer le cerveau.**

On conserve :
- `AudioAnalyzer` (audio_analyzer.py) comme **extracteur bas-niveau** : capture WASAPI, FFT, bandes bass/mid/treble, détection d'onsets (kick/snare/hat) avec timestamps + intensités, énergies. Sa sortie `snapshot()` est l'entrée du nouveau moteur.
- `AutoLightService` + `attach_engine()` + l'overlay `(universes, now)` installé via `engine.set_autolight_overlay(...)` + `on_rig_changed()` + le flux `apply_settings()`.

On remplace la **couche d'intelligence** (aujourd'hui : compteur de mesures naïf dans l'analyzer + `MusicDirector`/`DirectorOverlay` réactifs) par une pile claire.

## Nouvelle pile de modules

```
audio_analyzer.py          (conservé)   FFT, bandes, onsets kick/snare/hat, énergies
        │ snapshot() par frame
        ▼
autolight_beatgrid.py      (NOUVEAU)    ⟵ CŒUR, pièce manquante
        │   Tempo (octave-fold → 122-150), PHASE-LOCK (PLL) sur les onsets,
        │   beat_index, bar (4/4), downbeat estimé, phrase auto (8/16/32),
        │   phrase_index, bars_to_phrase_end, anticipation/build, confiance.
        ▼
autolight_brain.py         (NOUVEAU)    Décision : combine beat-grid + structure
        │   + métadonnées (genre/BPM/clé/structure DB) + mémoire → intent,
        │   dynamique (contraste, breakdown, anticipation drop), garde-fous.
        ▼
autolight_show.py          (NOUVEAU)    Rendu : rôles fixtures (hybride), topologie
        │   (miroir/chases/clusters), sous-ensembles selon énergie, couleur
        │   (mood DB + harmonie auto), focus strobe, positions « public » + invert.
        ▼
overlay (universes, now)   (adapté)     écrit les canaux DMX
```

Modules de support :
- `autolight_topology.py` (conservé/étendu) : miroir G/D, clusters/zones, ordre spatial — déjà présent, à réutiliser pour 129+ fixtures multi-univers.
- `music_sources.py` (étendu) : lookup métadonnées multi-sources en cascade (MusicBrainz/AcousticBrainz, Deezer, GetSongBPM) → genre, BPM officiel, clé, sections.
- `autolight_memory.json` (conservé) : structure par morceau + préférences.

## Contrat de données : `BeatGrid`

Entrée (par frame) : `observe(now, snapshot)` où `snapshot` = sortie de `AudioAnalyzer`.
Plus, hors bande : `set_reference_bpm(bpm, source)` (source `db`|`tap`|`None`).

État exposé (`state()`), consommé par le brain et l'UI « vue DJ complète » :

| Champ | Sens |
|---|---|
| `bpm` | tempo courant (octave-foldé vers 122-150 « ressenti ») |
| `bpm_source` | `db` / `tap` / `audio` |
| `locked` | grille verrouillée (phase stable) |
| `confidence` | 0–1 (variance de phase + accord des onsets) |
| `beat_index` | index de beat monotone depuis le reset |
| `beat_in_bar` | 0–3 (position dans la mesure, 0 = downbeat) |
| `bar_index` | index de mesure monotone |
| `phrase_len` | longueur de phrase détectée (8/16/32, défaut 16) |
| `phrase_index` | index de phrase |
| `bar_in_phrase` | 0..phrase_len-1 |
| `bars_to_phrase_end` | mesures restantes avant la fin de phrase (anticipation) |
| `beat_phase` | 0–1 position fractionnaire dans le beat courant |
| `since_beat_s` / `to_next_beat_s` | timing fin |
| `building` | montée détectée (énergie croissante en fin de phrase) |

Priorité d'autorité BPM (décision utilisateur) : **DB > tap ? non** → en fait :
`tap` écrase tout ; sinon `db` fait référence ; sinon `audio`. La **phase** vient
toujours des onsets audio.

## Algorithme `BeatGrid` (résumé)

1. **Tempo** : période (s/beat) issue de la source de référence, **octave-folding**
   (×½, ×2) pour tomber dans la plage ressentie 122-150. Sans référence fiable,
   estimation par intervalles d'onsets, même folding.
2. **Phase-lock (PLL)** : prédit `next_beat_t`. Quand un onset tombe près d'un beat
   attendu, corrige la phase (gain élevé au début → **lock < 2 s**, puis gain réduit
   pour la stabilité). Petite adaptation de période si pas de référence dure.
3. **Comptage** : à chaque franchissement de beat → `beat_index++`, MAJ bar/phrase.
4. **Downbeat** : histogramme EMA de l'intensité kick par position de mesure ; la
   position au kick le plus fort devient le « 1 » (avec hystérésis anti-bascule).
5. **Phrase** : série d'énergie par mesure → autocorrélation parmi {8,16,32} →
   `phrase_len` (défaut 16). `bars_to_phrase_end` alimente l'anticipation.
6. **Reset** : `reset()` au changement de morceau / seek (remet phase, compteurs,
   histogrammes). Pas de dérive à vie comme le compteur actuel.

Pur Python (math seul) → **testable hors audio** avec des trains d'onsets synthétiques
(c'est la validation « morceaux de référence » côté unité).

## Ordre de construction (incrémental, testé à chaque étape)

1. ✅/🚧 **`autolight_beatgrid.py` + tests** ← on commence ici (cœur, le plus cassé).
2. `music_sources.py` : lookup métadonnées multi-sources (genre/BPM/clé/structure).
3. `autolight_brain.py` : décision intent + dynamique + anticipation + garde-fous.
4. `autolight_show.py` : rendu rôles/topologie/couleur/focus/positions.
5. Brancher dans `AutoLightService` (remplacer `DirectorOverlay` par la nouvelle pile,
   garder un repli), retirer le pipeline « effects » hérité.
6. UI « vue DJ complète » + mini-panel garde-fous.
7. Validation : morceaux de référence (BPM/mesures/sections mesurés) + live.
