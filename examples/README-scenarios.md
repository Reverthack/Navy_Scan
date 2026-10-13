# Scénarios de test

| Commande (Windows) | Résultat attendu |
|---|---|
| `py scripts\cs.py simulate` | Une cible humaine approche la zone restreinte sans AIS : alarme **critique**, commande PTZ, vignette. |
| `py scripts\cs.py simulate-nav` | Un navire AIS suit la cible : alarme **info** « navire identifié ». |
| `py scripts\cs.py smoke` | Vérifie automatiquement alarme, criticité, vignette et PTZ sur un hub temporaire. |

Pour une lecture rapide : lancer `py scripts\cs.py run` dans un terminal, puis `py scripts\cs.py simulate` dans un autre, et ouvrir http://127.0.0.1:8000/.

Sur Linux ou macOS, remplacer `py scripts\cs.py` par `python3 scripts/cs.py`.
