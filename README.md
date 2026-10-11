# Lecteur

Un lecteur de PDF à voix haute, auto-hébergé. On importe ses livres en PDF, on clique sur une phrase, et la voix de Google Traduction (accent de France) lit la suite pendant que chaque phrase est surlignée. La lecture reprend là où on s'était arrêté, sur n'importe quel appareil.

L'accès est fermé : chacun peut demander un compte, mais seul l'administrateur peut le valider.

![Bibliothèque](docs/captures/bibliotheque.png)

## Fonctionnalités

- **Bibliothèque personnelle** : chaque compte a ses propres livres, avec une couverture générée à partir du titre et une barre de progression. Une carte « Reprendre la lecture » mène directement au dernier livre ouvert. Import par glisser-déposer, jusqu'à 50 Mo par PDF.
- **Lecture à voix haute** : la phrase en cours est surlignée et suivie à l'écran. On clique sur n'importe quelle phrase pour lire à partir de là. Vitesse de 0,8× à 5×, sans changer la hauteur de la voix. Les deux phrases suivantes sont préparées à l'avance pour éviter les blancs.
- **Progression enregistrée** : la phrase atteinte et la vitesse choisie sont sauvegardées pour chaque livre.
- **Comptes validés par l'admin** : inscription libre, mais connexion impossible tant que l'admin n'a pas validé le compte. Une révocation déconnecte la personne immédiatement.
- **Statistiques** : temps d'écoute réel (pauses exclues), minutes par jour sur 30 jours, livres en cours et terminés, jours d'affilée, livres les plus écoutés. Chacun voit les siennes ; l'admin voit celles de tout le monde.
- **Extraction propre du texte** : les paragraphes sont reconstitués, les mots coupés en fin de ligne recollés, les numéros de page supprimés et les ligatures (ﬁ, ﬃ) remplacées par les lettres correspondantes.

## Captures

| Lecture | Statistiques |
|---|---|
| ![Lecture](docs/captures/lecteur.png) | ![Statistiques](docs/captures/statistiques.png) |

| Connexion | Validation des comptes |
|---|---|
| ![Connexion](docs/captures/connexion.png) | ![Admin](docs/captures/admin.png) |

| Mobile : bibliothèque | Mobile : lecture |
|---|---|
| ![Bibliothèque sur mobile](docs/captures/mobile-bibliotheque.png) | ![Lecture sur mobile](docs/captures/mobile-lecteur.png) |

*Captures réalisées avec des comptes fictifs et des classiques du domaine public.*

## Démarrage rapide

Il faut Python 3.10 ou plus récent.

```bash
pip install -r requirements.txt
python serveur.py --admin <identifiant>   # crée le compte admin (le mot de passe est demandé)
python serveur.py                         # http://127.0.0.1:8765
```

Sous Windows, `Lancer.bat` démarre le serveur et ouvre le navigateur.

Les autres comptes se créent depuis la page de connexion, puis se valident sur la page **Admin**.

## Mise en production

Le serveur n'écoute que sur `127.0.0.1:8765`. Pour l'exposer, placez-le derrière un proxy HTTPS et définissez `LECTEUR_HTTPS=1` : les cookies de session reçoivent alors l'attribut `Secure`.

Exemple avec [Tailscale Funnel](https://tailscale.com/kb/1223/funnel), qui fournit une adresse HTTPS publique sans ouvrir de port :

```bash
tailscale funnel --bg 8765
```

Exemple de service systemd (code dans `/opt/lecteur`, exécuté par un utilisateur dédié `lecteur`) :

```ini
[Unit]
Description=Lecteur PDF à voix haute
After=network-online.target
Wants=network-online.target

[Service]
User=lecteur
Group=lecteur
WorkingDirectory=/opt/lecteur
Environment=LECTEUR_HTTPS=1 PYTHONUTF8=1
ExecStart=/opt/lecteur/venv/bin/python serveur.py
Restart=always
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/opt/lecteur/donnees
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

Toutes les données sont stockées dans `donnees/` : la base SQLite `lecteur.db` et les PDF importés dans `livres/`. Sauvegarder ce dossier suffit. La base est créée et mise à jour automatiquement au démarrage.

## Sécurité

- Mots de passe hachés avec scrypt et un sel par compte.
- Cookie de session `HttpOnly`, `SameSite=Lax`, et `Secure` derrière HTTPS. Les sessions durent 30 jours.
- Blocage de 15 minutes après 5 mauvais mots de passe sur un même identifiant.
- Seul le dossier `public/` est servi : la base et le code source ne sont pas téléchargeables.
- Chaque requête vérifie le propriétaire : un compte ne peut ni lire, ni modifier, ni voir les statistiques des livres d'un autre.

## Structure

| Fichier | Rôle |
|---|---|
| `serveur.py` | Serveur HTTP (bibliothèque standard), routes de l'API, synthèse vocale |
| `base.py` | Schéma SQLite, comptes, sessions, livres, progression, statistiques |
| `extraction.py` | Extraction du texte des PDF en paragraphes lisibles |
| `public/` | Interface : connexion, bibliothèque, lecteur, statistiques, admin |
| `test_serveur.py` | Test de bout en bout de l'API, sur une base jetable |

## Tests

```bash
python test_serveur.py
```

## Limites

- La voix passe par le service non officiel de Google Traduction (via [gTTS](https://github.com/pndurette/gTTS)). C'est parfait pour un usage personnel, mais Google peut limiter un usage intensif.
- Les PDF scannés (images sans texte) ne sont pas lisibles : il faudrait d'abord les passer à l'OCR.
