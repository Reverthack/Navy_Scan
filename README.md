# Surveillance côtière — prototype v0.3 (machine de test Windows)

Chaîne complète : **détection → zone → confirmation → AIS → vitesse → alarme → vignette → PTZ → supervision**.
Fonctionne **sans caméra ni antenne AIS** grâce au simulateur.

```
edge/simulator.py  ──HTTP──▶  hub/app.py (FastAPI + SQLite)  ◀──UDP── AIS-catcher (RTL-SDR, optionnel)
edge/collector.py                │  rules.py : zone, confirmation, anti-rebond, vitesse, AIS
edge/spot_detector.py (edge)     ├─ ptz.py : pilotage slew-to-cue (simulé pour l'instant)
                                 └─ interface web  http://127.0.0.1:8000/
```

## Installation sur Windows

Prérequis : **Python 3.10 ou plus** (https://www.python.org/downloads/). Pendant l'installation, cocher
« Add python.exe to PATH » et, si proposé, « py launcher ».

Ouvrir un terminal (PowerShell ou invite de commandes) dans le dossier `cote-surveillance-v3`, puis :

```powershell
py scripts\cs.py install     # venv, dépendances, clé API dans .env, tests
py scripts\cs.py run         # hub sur http://127.0.0.1:8000/  (laisser ce terminal ouvert)
```

Dans un **second** terminal, dans le même dossier :

```powershell
py scripts\cs.py simulate        # intrusion humaine en zone restreinte, sans AIS → alarme critique
py scripts\cs.py simulate-nav    # navire AIS qui suit la cible → navire identifié
py scripts\cs.py smoke           # vérification automatique de bout en bout (hub temporaire)
```

Puis ouvrir http://127.0.0.1:8000/ dans le navigateur.

Si Windows demande d'autoriser Python à accéder au réseau, accepter pour les réseaux privés.
Le hub n'écoute que sur `127.0.0.1` : il n'est pas accessible depuis les autres machines.

Pour afficher la liste des commandes : `py scripts\cs.py` (sans argument).

### Sur Linux ou macOS

Même commandes, avec `python3 scripts/cs.py <commande>`.

## Ce qui est implémenté

| Fonction | Où | État |
|---|---|---|
| Localisation (lat/lon ou cap + distance) | `hub/rules.py`, `hub/geo.py` | Complet |
| Zones restreintes et surveillées | `config.json` | Complet |
| Confirmation de piste (N détections / fenêtre) | `hub/rules.py` | Complet |
| Anti-rebond (pas de répétition d'alarme) | `hub/rules.py` (`cooldown_s`) | Complet |
| Vitesse de piste, drapeau « vitesse élevée » | `hub/rules.py` (`max_speed_mps`) | Complet |
| Classe de cible (`humain`, `embarcation`) et drapeau « humain » | `hub/rules.py` | Complet, classe fournie par la source |
| Corrélation AIS (rayon, fraîcheur) et type de navire | `hub/rules.py`, `hub/ais.py` | Complet |
| Vignettes par alarme (upload et affichage) | `hub/app.py`, `hub/store.py` | Complet |
| Pilotage PTZ slew-to-cue | `hub/ptz.py` | **Simulé** (enregistrement, pas de commande réelle) |
| Interface de supervision | `hub/static/` | Complète, testée côté serveur |
| Détection de petites cibles | `edge/spot_detector.py` | **Base de travail**, à valider sur images réelles |
| Classification automatique par IA | — | Non inclus (nécessite caméra et données) |

## Configuration

- `config.json` : sites, zones, règles, AIS, PTZ, et caméras **initiales**.

### Gérer les caméras

Les caméras se gèrent depuis l'interface, dans le panneau **Caméras** (bouton « + Ajouter »), ou en cliquant sur une caméra de la carte ou de la liste. Chaque modification demande la clé API.

Les caméras sont stockées dans la base (`data/hub.db`). `config.json` ne sert qu'à la première exécution : si la base ne contient aucune caméra, celles de `config.json` y sont importées. Ensuite, c'est la base qui fait foi.

Champs d'une caméra :

| Champ | Rôle |
|---|---|
| Identifiant | Unique, lettres, chiffres, `-` et `_` (40 caractères max). Non modifiable. Doit correspondre au `camera_id` envoyé par la source. |
| Nom | Affiché dans l'interface |
| Site | Le site auquel la caméra est rattachée |
| Latitude, longitude | Position de la caméra sur la carte |
| Type | Information (panoramique, PTZ, bullet, visible) |
| Zones couvertes | Une, plusieurs, ou aucune (= toutes les zones du site). Seules les zones couvertes produisent des alarmes pour cette caméra. |
| Pilotage PTZ | Si coché, une alarme critique déclenche une commande d'orientation pour cette caméra |
| Activée | Si décochée, les détections de cette caméra sont refusées (403) |

Statut affiché : **en ligne** (signal il y a moins de 5 min), **hors ligne**, **silencieuse** (jamais de signal), **désactivée**.

Règles appliquées au hub :
- une détection d'une caméra **non déclarée** est refusée (404) ;
- une détection d'une caméra rattachée à **un autre site** est refusée (422) ;
- une détection d'une caméra **désactivée** est refusée (403).

Les identifiants de connexion d'une caméra réelle (futur pilotage ONVIF) ne doivent pas être saisis dans l'interface : ils iront dans `.env`.
- `.env` : clé API et chemin de la base. Créé par `install`. Ne pas le partager ni le commiter.

Points clés de `config.json` :
- `rules.confirm_hits` / `confirm_window_s` : nombre de détections et fenêtre pour confirmer une piste.
- `rules.cooldown_s` : délai minimal entre deux alarmes d'une même piste.
- `rules.max_speed_mps` : au-delà, drapeau « vitesse élevée ».
- `ptz.enabled` / `ptz.backend` : `simulated` tant qu'aucune caméra PTZ n'est choisie.
- `ais.enabled` : écoute UDP du port 10110.

Les coordonnées du site et des zones sont fictives (près de Brest) : à remplacer par le site réel.

### Fond de carte

Le bloc `map` de `config.json` décrit les fournisseurs. Deux sont prévus :

- **CARTO** (fond clair « voyager ») : utilisé si `MAP_TILE_KEY` est défini dans `.env`.
- **OpenStreetMap** (assombri par CSS) : utilisé sinon, sans clé.

Pour activer CARTO sur votre machine, ajouter dans `.env` :

```
MAP_TILE_KEY=votre_clé_carto
```

Puis relancer le hub. La clé n'est jamais écrite dans `config.json` ni dans le projet.

Points à connaître :

- **La clé est visible dans le navigateur** : les tuiles sont chargées par le navigateur, la clé figure donc dans l'URL. C'est inhérent aux fonds de carte à clé. Une clé dédiée à ce prototype limite le risque ; vérifier dans le tableau de bord CARTO les restrictions disponibles.
- **Attribution obligatoire** : « © OpenStreetMap contributors © CARTO », affichée automatiquement.
- **OpenStreetMap** interdit les usages intensifs : acceptable pour ce prototype, pas pour un service en production.

## API

| Méthode | Route | Auth | Rôle |
|---|---|---|---|
| GET | `/api/status` | non | Santé, version, compteurs |
| POST | `/api/events` | clé | Réception d'une détection |
| GET | `/api/alarms?limit=` | non | Liste des alarmes |
| POST | `/api/alarms/{id}/ack` | clé | Acquittement |
| POST | `/api/alarms/{id}/snapshot` | clé | Vignette (`image/jpeg` ou `image/png`, 2 Mo max) |
| GET | `/api/alarms/{id}/snapshot` | non | Lecture de la vignette |
| GET | `/api/cameras` | non | Caméras déclarées, avec leur statut |
| POST | `/api/cameras` | clé | Déclarer une caméra (409 si l'identifiant existe) |
| PUT | `/api/cameras/{id}` | clé | Modifier une caméra |
| DELETE | `/api/cameras/{id}` | clé | Supprimer une caméra |
| GET | `/api/vessels` | non | Navires AIS récents |
| GET | `/api/sites` | non | Sites et zones |
| GET | `/api/ptz/commands` | non | Historique des commandes PTZ |

Authentification : en-tête `X-API-Key`.

## AIS (réception réelle)

Configurer AIS-catcher pour envoyer sa sortie JSON en UDP vers `127.0.0.1:10110`. Les options exactes dépendent de la version : à vérifier dans sa documentation. Les champs attendus sont `mmsi`, `lat`, `lon` pour la position, et `shipname`, `ship_type` pour les données statiques. Si les noms de champs diffèrent, adapter `hub/ais.py` (`ingest_line`).

Sous Windows, si le port 10110 est déjà utilisé, le hub journalise une erreur explicite au démarrage et continue sans AIS.

## Limites à connaître avant tout test terrain

- **Clé API unique** partagée par tous les composants : à remplacer par une clé par site.
- **Pas de TLS** dans le hub : à mettre derrière un reverse proxy si accès réseau.
- **PTZ simulé** : aucune commande n'est envoyée à une caméra réelle.
- **Détection edge** : la base de travail n'a pas été comparée à des images réelles. Ce n'est pas l'implémentation de l'article HGH.
- **Classification** : fournie par la source de détection, pas par le hub.
- **Cadre légal** : autorisations et conservation des données vidéo à valider avant mise en service.

## Dépannage Windows

| Symptôme | Cause probable | Solution |
|---|---|---|
| `py` introuvable | Python non installé ou sans launcher | Réinstaller Python en cochant « py launcher », ou utiliser `python` à la place de `py` |
| `Environnement absent` | `install` pas encore lancé | `py scripts\cs.py install` |
| Erreur de script bloqué | Stratégie d'exécution PowerShell | Lancer `py scripts\cs.py ...` (pas de script `.ps1` à exécuter) |
| `Impossible d'écouter l'AIS` | Port 10110 déjà utilisé | Fermer l'autre programme ou changer `ais.udp_port` dans `config.json` (et la configuration AIS-catcher) |
| Carte grise, alerte « Fond de carte … indisponible » | Pas d'accès Internet, ou `MAP_TILE_KEY` absente / incorrecte dans `.env` | Vérifier la clé dans `.env` puis relancer le hub. Les alarmes et navires restent affichés. |
| Carte absente après une mise à jour | Ancien `app.js` gardé par le navigateur | Recharger la page avec Ctrl+F5 |
| « aucun message reçu » dans le panneau Navires | Pas d'antenne AIS ni de simulation | Sans antenne : `py scripts\cs.py simulate-nav` (dans un second terminal) |
| « AIS indisponible : port UDP … inaccessible » | Port 10110 déjà utilisé | Fermer le programme concerné, ou changer `ais.udp_port` |
| 401 à l'acquittement | Clé API saisie incorrecte | Vérifier la valeur de `CS_API_KEY` dans `.env` |
| `DLL load failed` … « stratégie de contrôle d'application a bloqué ce fichier » | Une politique de sécurité Windows (App Control, AppLocker, Smart App Control) bloque une bibliothèque de numpy | Le hub et l'interface fonctionnent sans elle : le test `test_spot_detector` est alors ignoré. Pour le détecteur edge, demander à l'administrateur informatique d'autoriser numpy, ou exécuter ce module sur une autre machine. Ne pas désactiver la protection. |

## Dépendances

- `requirements.txt` : hub, interface et tests (FastAPI, Uvicorn, HTTPX). Obligatoire.
- `requirements-edge.txt` : numpy et pillow, pour le détecteur de cibles et les vignettes du simulateur. Optionnel : `install` continue si elles sont bloquées.
