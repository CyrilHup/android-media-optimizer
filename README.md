# Android Media Optimizer

Outil local pour réduire le stockage des photos et vidéos d’un téléphone Android, avec tableau de bord et reprise après interruption. Le superviseur tourne en arrière-plan sur Windows; l’interface est accessible sur http://127.0.0.1:8878/phone-video-compression-batches/progress.html.

## Fonctionnement

- Vidéos : débit adapté à la source, résolution plafonnée à 1080p, voies NVIDIA NVENC et AMD AMF; voie CPU optionnelle.
- Photos : JPEG vers WebP, PNG sans perte; repli JPEG conservant les métadonnées Adobe/Photoshop; parcours Ultra HDR séparé sans perte.
- Mesures de qualité, dates et métadonnées, sauvegarde originale vérifiée par SHA-256, transfert temporaire, validation du fichier et de l’index multimédia avant nettoyage.
- Journaux persistants, checkpoints d’encodage, pause/reprise, découverte du port de débogage sans fil et reprise des coupures reconnues.
- Aucun service cloud pour traiter les médias. GitHub ne reçoit que le code de ce dépôt.

## État du projet

Il s’agit de l’extraction d’un outil personnel en cours d’utilisation, pas d’un produit Android prêt à installer. Les files d’attente, inventaires, autorisations, sauvegardes, rapports et médias sont volontairement absents du dépôt. **Un clone ne contient aucune autorisation de modifier un téléphone.** Pour un autre appareil, il faut créer et vérifier son propre inventaire et ses propres plans avant d’autoriser les remplacements. Ne fabriquez pas une autorisation en recopiant celle d’un autre appareil.

Les scripts historiques de nettoyage d’applications et les captures du téléphone ne sont pas distribués. Les sources restent dans `work/phone-audit/` pour préserver les chemins attendus par les checkpoints.

## Environnement

Windows, Python 3.12, Pillow (version testée épinglée dans `requirements.txt`). Node.js sert uniquement au test hors ligne du tableau de bord. Les tests unitaires utilisent des données synthétiques et des téléphones simulés.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item local-config.example.json local-config.json
```

Dans `local-config.json`, renseigner `phone_serial` avec le numéro matériel de votre appareil déjà jumelé à ADB, et `photo_python` avec le Python contenant Pillow. `PHONE_HARDWARE_SERIAL` peut remplacer la valeur du fichier. La configuration réelle est ignorée par Git. Le débogage USB ou sans fil doit être autorisé sur le téléphone; être simplement connecté au Wi-Fi ne suffit pas.

Outils natifs attendus par les constantes actuelles des modules (à installer localement ou à adapter dans le code) :

| Outil | Emplacement actuel |
| --- | --- |
| Android platform-tools | `work/android-tools/platform-tools/adb.exe` |
| FFmpeg et ffprobe avec les encodeurs voulus et les filtres de qualité | `C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin/` |
| cwebp, webpmux, jpegtran | `C:/msys64/mingw64/bin/` |
| Harnais Ultra HDR et ses DLL | `work/hdr-tools/build/uhdr_roundtrip.exe` |

Le harnais C++ est fourni; les sources et binaires tiers ne sont pas inclus. Il utilise l’API de [google/libultrahdr](https://github.com/google/libultrahdr), commit testé `47d9f00726adba87e3f04059a50a8b34492e057a`. Construire avec les métadonnées XMP et ISO activées (`UHDR_WRITE_XMP=ON`, `UHDR_WRITE_ISO=ON`), puis lier `uhdr_roundtrip.cpp` à cette bibliothèque. Respecter les licences des outils téléchargés séparément.

## Démarrer et tester

Après préparation et approbation des plans locaux, lancer `Start-AndroidMediaOptimizer.cmd`. Le lanceur utilise `.venv` si disponible, sinon un Python installé; un chemin `service_python` peut aussi être renseigné dans la configuration locale. Le panneau sert les boutons Pause, Reprendre et Arrêter au prochain point sûr.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s work/phone-audit -p "test_*.py"
node work/phone-audit/verify_dashboard_eta.cjs
```

Les vérifications supplémentaires `verify_photo_analysis.py` et `verify_webp_metadata.py` nécessitent Pillow et les outils WebP attendus. Les tests ne sont pas rejoués pour chaque média; le traitement applique seulement ses contrôles d’intégrité et de qualité.

## Structure et données locales

- `work/phone-audit/` : moteur, transactions, reprises et tests.
- `web/progress.html` : source du panneau, copiée vers le dossier servi au démarrage.
- `work/hdr-tools/uhdr_roundtrip.cpp` : vérification/reconditionnement Ultra HDR.
- `outputs/`, autres sous-dossiers `work/`, `local-config.json` : données locales non versionnées. Ne les ajoutez pas de force au dépôt.

Conserver le PC éveillé et le téléphone joignable pendant le traitement. Une erreur d’intégrité arrête le traitement concerné avec les sauvegardes disponibles; les erreurs réseau reconnues sont reprises à partir du journal.
