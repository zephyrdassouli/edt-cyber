# Suivi de formation — EDT IMT Atlantique → xlsx

Deux scripts :

- `ics_calendar.py` : récupère et parse l'EDT (lien ICS "PASSCAL"), classe chaque cours par code UE.
- `fill_timesheet.py` : utilise `ics_calendar.py` pour remplir le fichier de suivi de formation (`DASSOULI_Zephyr.xlsx`) pour une semaine donnée.

Les deux fichiers doivent rester **dans le même dossier**.

## 1. Installation

```bash
pip install -r requirements.txt
```


## 2. Configurer le lien ICS

Ne pas mettre le lien en dur dans un script (il contient ton token personnel). Le passer en variable d'environnement :

```bash
export ICS_CALENDAR_URL="https://inpass.imt-atlantique.fr/passcal/getics?login=...&check=..."
```
Le lien est trouvable sur votre profil PASS dans la section "Export Agenda ICS"

À refaire à chaque nouvelle session de terminal (ou à ajouter dans `.bashrc`/`.zshrc`).

## 3. Remplir le suivi d'une semaine

```bash
python fill_timesheet.py --week 37 --input DASSOULI_Zephyr.xlsx
```

- `--week` : numéro de semaine ISO (obligatoire).
- `--input` : le fichier xlsx modèle (obligatoire).
- Résultat écrit dans `DASSOULI_Zephyr_S37.xlsx` (à côté de l'input) — le fichier original n'est jamais modifié. Pour choisir un autre nom : `--output mon_fichier.xlsx`.

Le script affiche un résumé des séances trouvées et de leur code UE, puis écrit :
- `Semaine du` / `au` / `N° de semaine`
- une ligne par cours (date, code UE, horaires, pause, durée de formation)
- le total d'heures de la semaine

### Options utiles

| Option | Effet |
|---|---|
| `--year 2026` | Force l'année si une semaine existe sur 2 années scolaires (rare, script te prévient si besoin) |
| `--force-refresh` | Ignore le cache local et retélécharge l'EDT |
| `-v` | Logs détaillés (debug) |

### Cas particuliers gérés automatiquement

- **Cours fractionnés** : si PASS découpe un cours en plusieurs créneaux du même code UE le même jour, ils sont fusionnés en une seule ligne, la pause entre eux étant déduite du temps de formation.
- **Code UE non reconnu** → classé `DIV CYBER`.
- **Plus de 28 séances dans la semaine** (rare) → le script écrit ce qu'il peut et te prévient de ce qu'il n'a pas pu caser.
- **Pas de réseau** → retombe sur le dernier EDT téléchargé (cache local `schedule_cache.ics`, 15 min de fraîcheur par défaut).

## 4. Utiliser juste l'EDT (sans le xlsx)

```bash
python ics_calendar.py                    # résumé + répartition par code UE
python ics_calendar.py --json cours.json   # exporte les cours en JSON
python ics_calendar.py --next 5            # 5 prochains cours
```

## Dépannage

- **`ValueError: Aucun évènement trouvé pour la semaine ISO N`** → l'EDT ne contient encore rien pour cette semaine (pas encore publié côté IMT Atlantique), ou `ICS_CALENDAR_URL` n'est pas configurée.
- **Un cours tombe dans `DIV CYBER` alors qu'il ne devrait pas** → l'intitulé réel du cours (`SUMMARY` dans l'ICS) ne contient ni le code (ex. `NETCRY`) ni un des mots-clés attendus. Ouvrir `ics_calendar.py`, chercher `_UE_CATEGORIES`, et ajouter l'intitulé exact observé à la liste du bon code.
- **Erreur `403`/réseau lors du fetch** → vérifier que `ICS_CALENDAR_URL` est toujours valide (le token `check=` peut expirer).