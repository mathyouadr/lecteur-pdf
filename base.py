"""Base SQLite : comptes, sessions, livres et progression de lecture. Seul module qui touche à la base
et aux fichiers de donnees/ (PDF, livres audio, couvertures)."""
import hashlib
import hmac
import json
import secrets
import shutil
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from extraction import phrases as decouper

DONNEES = Path(__file__).parent / "donnees"
BASE = DONNEES / "lecteur.db"
LIVRES = DONNEES / "livres"            # LIVRES/<uid>/<lid>.pdf
AUDIO = DONNEES / "audio"              # AUDIO/<lid>/<n° de phrase>.mp3 (ou .wav sans ffmpeg)
COUVERTURES = DONNEES / "couvertures"  # COUVERTURES/<lid>.<jpg|png|webp>
VOIX = DONNEES / "voix"                # VOIX/<id>.mp3 : démo de chaque voix du catalogue
DUREE_SESSION = 30 * 86400
EXTENSIONS_AUDIO = ("mp3", "wav")
DUREE_LIVRE = 30 * 86400           # un livre (PDF, audio, couverture) part 1 mois après sa dernière écoute
DUREE_PROGRESSION = 60 * 86400     # sa position reste gardée 2 mois après cette dernière écoute

# Voix de départ du catalogue (vocabulaire de « voice design » d'OmniVoice), modifiable depuis le panel admin
VOIX_DEPART = [("Homme grave", "male, middle-aged, low pitch", "42"), ("Femme douce", "female, young adult", "7"),
               ("Homme jeune", "male, young adult", "11"), ("Femme âgée", "female, elderly", "23")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS utilisateurs (
    id INTEGER PRIMARY KEY,
    identifiant TEXT NOT NULL UNIQUE COLLATE NOCASE,
    empreinte TEXT NOT NULL,
    statut TEXT NOT NULL DEFAULT 'en_attente' CHECK (statut IN ('en_attente', 'valide', 'refuse')),
    admin INTEGER NOT NULL DEFAULT 0,
    cree_le INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    jeton TEXT PRIMARY KEY,
    utilisateur INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
    expire INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS livres (
    id INTEGER PRIMARY KEY,
    utilisateur INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
    titre TEXT NOT NULL,
    paragraphes TEXT NOT NULL,
    phrase INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    vitesse REAL NOT NULL DEFAULT 1,
    lu_le INTEGER,
    ajoute_le INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ecoutes (
    utilisateur INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
    livre INTEGER REFERENCES livres(id) ON DELETE SET NULL,
    titre TEXT NOT NULL,
    jour TEXT NOT NULL,
    secondes REAL NOT NULL DEFAULT 0,
    phrases INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ecoutes_cle ON ecoutes (utilisateur, livre, jour);
CREATE TABLE IF NOT EXISTS voix (
    id INTEGER PRIMARY KEY,
    nom TEXT NOT NULL,
    instruct TEXT NOT NULL,
    seed TEXT NOT NULL,
    demo TEXT                          -- extension du fichier de démo, NULL tant qu'elle n'est pas générée
);
-- Position d'un livre supprimé (1 mois sans écoute) : retrouvée si la personne réimporte le même PDF
CREATE TABLE IF NOT EXISTS positions_gardees (
    utilisateur INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
    empreinte TEXT NOT NULL,
    phrase INTEGER NOT NULL,
    total INTEGER NOT NULL,
    vitesse REAL NOT NULL,
    lu_le INTEGER NOT NULL,
    PRIMARY KEY (utilisateur, empreinte)
);
"""

# Colonnes ajoutées après la première version : créées au démarrage si la base est plus ancienne
COLONNES_LIVRES = {
    "phrases": "TEXT",                  # JSON : une liste de phrases par paragraphe (découpage serveur)
    "couverture": "TEXT",               # extension de l'image importée, NULL = couverture générée
    "empreinte": "TEXT",                # SHA-256 du PDF, pour reconnaître un livre réimporté
    "voix": "INTEGER",                  # voix choisie pour le livre audio (id du catalogue)
    "audio_etat": "TEXT",               # NULL, attente, encours, pause, pret, erreur
    "audio_faits": "INTEGER NOT NULL DEFAULT 0",
    "audio_erreur": "TEXT",
}


def _sql(requete, params=()):
    """Exécute une requête dans sa propre transaction ; renvoie (lignes, id inséré)."""
    c = sqlite3.connect(BASE, timeout=10)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        with c:
            cur = c.execute(requete, params)
            return [dict(l) for l in cur.fetchall()], cur.lastrowid
    finally:
        c.close()


def initialiser():
    for dossier in (LIVRES, AUDIO, COUVERTURES, VOIX):
        dossier.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(BASE)
    c.executescript(SCHEMA)
    existantes = {l[1] for l in c.execute("PRAGMA table_info(livres)")}
    for nom, definition in COLONNES_LIVRES.items():
        if nom not in existantes:
            c.execute(f"ALTER TABLE livres ADD COLUMN {nom} {definition}")
    # Livres importés avant le découpage serveur : on découpe, et on recale la phrase atteinte
    # (l'ancien découpage du navigateur ne tombait pas exactement aux mêmes endroits)
    for lid, paras, phrase, total in c.execute(
            "SELECT id, paragraphes, phrase, total FROM livres WHERE phrases IS NULL").fetchall():
        decoupe = [decouper(p) for p in json.loads(paras)]
        n = sum(map(len, decoupe))
        if total:
            phrase = n if phrase >= total else min(round(phrase * n / total), n)
        c.execute("UPDATE livres SET phrases = ?, phrase = ?, total = ? WHERE id = ?",
                  (json.dumps(decoupe, ensure_ascii=False), phrase, n, lid))
    for lid, uid in c.execute("SELECT id, utilisateur FROM livres WHERE empreinte IS NULL").fetchall():
        pdf = LIVRES / str(uid) / f"{lid}.pdf"
        if pdf.exists():
            c.execute("UPDATE livres SET empreinte = ? WHERE id = ?", (_empreinte_pdf(pdf.read_bytes()), lid))
    if not c.execute("SELECT 1 FROM voix").fetchone():
        c.executemany("INSERT INTO voix (nom, instruct, seed) VALUES (?, ?, ?)", VOIX_DEPART)
    c.commit()
    c.close()


def _empreinte_pdf(pdf):
    return hashlib.sha256(pdf).hexdigest()


# ---------- Mots de passe ----------
def _empreinte(mdp, sel=None):
    sel = sel or secrets.token_bytes(16)
    h = hashlib.scrypt(mdp.encode(), salt=sel, n=2**14, r=8, p=1)
    return f"{sel.hex()}${h.hex()}"


def _mdp_correct(mdp, empreinte):
    sel, _ = empreinte.split("$")
    return hmac.compare_digest(_empreinte(mdp, bytes.fromhex(sel)), empreinte)


# ---------- Comptes ----------
def creer_utilisateur(identifiant, mdp, statut="en_attente", admin=False):
    """Renvoie l'id du compte, ou None si l'identifiant est déjà pris."""
    try:
        return _sql("INSERT INTO utilisateurs (identifiant, empreinte, statut, admin, cree_le) VALUES (?, ?, ?, ?, ?)",
                    (identifiant, _empreinte(mdp), statut, int(admin), int(time.time())))[1]
    except sqlite3.IntegrityError:
        return None


def promouvoir_admin(identifiant, mdp):
    """Crée le compte admin, ou promeut et réinitialise le mot de passe d'un compte existant."""
    if creer_utilisateur(identifiant, mdp, "valide", True) is None:
        _sql("UPDATE utilisateurs SET empreinte = ?, statut = 'valide', admin = 1 WHERE identifiant = ?",
             (_empreinte(mdp), identifiant))


def authentifier(identifiant, mdp):
    """Renvoie le compte si le mot de passe est bon (quel que soit son statut), sinon None."""
    lignes, _ = _sql("SELECT * FROM utilisateurs WHERE identifiant = ?", (identifiant,))
    if not lignes:
        _empreinte(mdp)  # même durée de calcul, pour ne pas révéler quels identifiants existent
        return None
    return lignes[0] if _mdp_correct(mdp, lignes[0]["empreinte"]) else None


def utilisateurs():
    return _sql("SELECT id, identifiant, statut, admin, cree_le FROM utilisateurs ORDER BY cree_le DESC")[0]


def definir_statut(uid, statut):
    _sql("UPDATE utilisateurs SET statut = ? WHERE id = ? AND admin = 0", (statut, uid))
    if statut != "valide":
        _sql("DELETE FROM sessions WHERE utilisateur = ?", (uid,))


def supprimer_utilisateur(uid):
    """Supprime un compte (jamais l'admin), ses sessions, ses livres et tous leurs fichiers."""
    if not _sql("SELECT 1 FROM utilisateurs WHERE id = ? AND admin = 0", (uid,))[0]:
        return False
    for l in _sql("SELECT id FROM livres WHERE utilisateur = ?", (uid,))[0]:
        supprimer_livre(None, l["id"])
    _sql("DELETE FROM utilisateurs WHERE id = ?", (uid,))  # sessions et écoutes : ON DELETE CASCADE
    shutil.rmtree(LIVRES / str(uid), ignore_errors=True)
    return True


# ---------- Sessions ----------
def ouvrir_session(uid):
    jeton = secrets.token_urlsafe(32)
    _sql("DELETE FROM sessions WHERE expire < ?", (int(time.time()),))
    _sql("INSERT INTO sessions VALUES (?, ?, ?)", (jeton, uid, int(time.time()) + DUREE_SESSION))
    return jeton


def compte_de_session(jeton):
    """Compte validé lié à ce jeton, ou None."""
    lignes, _ = _sql("""SELECT u.id, u.identifiant, u.admin FROM sessions s JOIN utilisateurs u ON u.id = s.utilisateur
                        WHERE s.jeton = ? AND s.expire > ? AND u.statut = 'valide'""", (jeton, int(time.time())))
    return lignes[0] if lignes else None


def fermer_session(jeton):
    _sql("DELETE FROM sessions WHERE jeton = ?", (jeton,))


# ---------- Livres ----------
# Dans les fonctions qui prennent uid : uid=None veut dire « l'admin, quel que soit le propriétaire ».
CHAMPS_LISTE = "id, titre, phrase, total, vitesse, lu_le, ajoute_le, couverture, voix, audio_etat, audio_faits"


def _proprio(uid, colonne="utilisateur"):
    return ("1", ()) if uid is None else (f"{colonne} = ?", (uid,))


def livres(uid):
    return _sql(f"""SELECT {CHAMPS_LISTE} FROM livres
                    WHERE utilisateur = ? ORDER BY COALESCE(lu_le, ajoute_le) DESC""", (uid,))[0]


def tous_les_livres():
    """Pour l'admin : tous les livres, avec leur propriétaire et la place qu'ils prennent sur le disque."""
    lignes = _sql(f"""SELECT l.utilisateur, u.identifiant, {", ".join("l." + c for c in CHAMPS_LISTE.split(", "))}
                      FROM livres l JOIN utilisateurs u ON u.id = l.utilisateur
                      ORDER BY u.identifiant, l.ajoute_le DESC""")[0]
    for l in lignes:
        fichiers = [LIVRES / str(l["utilisateur"]) / f"{l['id']}.pdf", *_fichiers_audio(l["id"])]
        if l["couverture"]:
            fichiers.append(COUVERTURES / f"{l['id']}.{l['couverture']}")
        l["octets"] = sum(f.stat().st_size for f in fichiers if f.exists())
    return lignes


def livre(uid, lid):
    """Livre complet (texte découpé en phrases par paragraphe), ou None s'il n'appartient pas à uid."""
    filtre, p = _proprio(uid, "l.utilisateur")
    lignes, _ = _sql(f"""SELECT l.*, u.identifiant proprietaire FROM livres l JOIN utilisateurs u ON u.id = l.utilisateur
                         WHERE l.id = ? AND {filtre}""", (lid, *p))
    if not lignes:
        return None
    l = lignes[0]
    l["paragraphes"] = json.loads(l.pop("phrases"))
    return l


def phrases_a_plat(lid):
    lignes, _ = _sql("SELECT phrases FROM livres WHERE id = ?", (lid,))
    return [p for para in json.loads(lignes[0]["phrases"]) for p in para] if lignes else []


def ajouter_livre(uid, titre, paragraphes, pdf):
    """Renvoie (id, repris) : repris=True si la position gardée d'un livre supprimé a été retrouvée."""
    decoupe = [decouper(p) for p in paragraphes]
    empreinte = _empreinte_pdf(pdf)
    gardee = _sql("SELECT phrase, total, vitesse FROM positions_gardees WHERE utilisateur = ? AND empreinte = ?",
                  (uid, empreinte))[0]
    phrase, vitesse, lu_le = (gardee[0]["phrase"], gardee[0]["vitesse"], int(time.time())) if gardee else (0, 1, None)
    lid = _sql("""INSERT INTO livres (utilisateur, titre, paragraphes, phrases, total, empreinte, phrase, vitesse, lu_le, ajoute_le)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (uid, titre, json.dumps(paragraphes, ensure_ascii=False), json.dumps(decoupe, ensure_ascii=False),
                sum(map(len, decoupe)), empreinte, phrase, vitesse, lu_le, int(time.time())))[1]
    if gardee:
        _sql("DELETE FROM positions_gardees WHERE utilisateur = ? AND empreinte = ?", (uid, empreinte))
    dossier = LIVRES / str(uid)
    dossier.mkdir(exist_ok=True)
    (dossier / f"{lid}.pdf").write_bytes(pdf)  # gardé pour pouvoir ré-extraire si l'extraction s'améliore
    return lid, bool(gardee)


def purger(maintenant=None):
    """Supprime les livres pas écoutés depuis 1 mois (leur position est gardée), puis les positions gardées
    depuis plus de 2 mois après la dernière écoute. Un livre audio en cours de génération n'est jamais supprimé."""
    maintenant = maintenant or time.time()
    vieux = _sql("""SELECT id, utilisateur, empreinte, phrase, total, vitesse, COALESCE(lu_le, ajoute_le) lu_le
                    FROM livres WHERE COALESCE(lu_le, ajoute_le) < ? AND COALESCE(audio_etat, '') != 'encours'""",
                 (maintenant - DUREE_LIVRE,))[0]
    for l in vieux:
        if l["empreinte"] and l["phrase"] > 0:
            _sql("""INSERT INTO positions_gardees VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (utilisateur, empreinte) DO UPDATE SET phrase = excluded.phrase,
                    total = excluded.total, vitesse = excluded.vitesse, lu_le = excluded.lu_le""",
                 (l["utilisateur"], l["empreinte"], l["phrase"], l["total"], l["vitesse"], l["lu_le"]))
        supprimer_livre(None, l["id"])
    _sql("DELETE FROM positions_gardees WHERE lu_le < ?", (maintenant - DUREE_PROGRESSION,))
    return len(vieux)


def sauver_progression(uid, lid, phrase, total, vitesse):
    _sql("UPDATE livres SET phrase = ?, total = ?, vitesse = ?, lu_le = ? WHERE id = ? AND utilisateur = ?",
         (phrase, total, vitesse, int(time.time()), lid, uid))


def supprimer_livre(uid, lid):
    filtre, p = _proprio(uid)
    lignes, _ = _sql(f"SELECT utilisateur, couverture FROM livres WHERE id = ? AND {filtre}", (lid, *p))
    if not lignes:
        return
    _sql("DELETE FROM livres WHERE id = ?", (lid,))
    (LIVRES / str(lignes[0]["utilisateur"]) / f"{lid}.pdf").unlink(missing_ok=True)
    shutil.rmtree(AUDIO / str(lid), ignore_errors=True)
    if lignes[0]["couverture"]:
        (COUVERTURES / f"{lid}.{lignes[0]['couverture']}").unlink(missing_ok=True)


# ---------- Couvertures ----------
def definir_couverture(uid, lid, image, extension):
    """image=None : retour à la couverture générée. Renvoie False si le livre n'appartient pas à uid."""
    filtre, p = _proprio(uid)
    lignes, _ = _sql(f"SELECT couverture FROM livres WHERE id = ? AND {filtre}", (lid, *p))
    if not lignes:
        return False
    if lignes[0]["couverture"]:
        (COUVERTURES / f"{lid}.{lignes[0]['couverture']}").unlink(missing_ok=True)
    if image is not None:
        (COUVERTURES / f"{lid}.{extension}").write_bytes(image)
    _sql("UPDATE livres SET couverture = ? WHERE id = ?", (extension if image is not None else None, lid))
    return True


def fichier_couverture(uid, lid):
    filtre, p = _proprio(uid)
    lignes, _ = _sql(f"SELECT couverture FROM livres WHERE id = ? AND {filtre}", (lid, *p))
    if not lignes or not lignes[0]["couverture"]:
        return None
    return COUVERTURES / f"{lid}.{lignes[0]['couverture']}"


# ---------- Livres audio ----------
def _fichiers_audio(lid):
    dossier = AUDIO / str(lid)
    return [f for f in dossier.iterdir() if f.suffix[1:] in EXTENSIONS_AUDIO] if dossier.is_dir() else []


def livre_visible(uid, lid):
    filtre, p = _proprio(uid)
    return bool(_sql(f"SELECT 1 FROM livres WHERE id = ? AND {filtre}", (lid, *p))[0])


def fichier_audio(uid, lid, n):
    """Fichier audio IA de la phrase n, ou None s'il n'est pas (encore) généré."""
    filtre, p = _proprio(uid)
    if not _sql(f"SELECT 1 FROM livres WHERE id = ? AND {filtre}", (lid, *p))[0]:
        return None
    return next((f for ext in EXTENSIONS_AUDIO if (f := AUDIO / str(lid) / f"{n}.{ext}").exists()), None)


def ecrire_audio(lid, n, donnees, extension):
    if not _sql("SELECT 1 FROM livres WHERE id = ?", (lid,))[0]:
        return  # livre supprimé pendant la génération de cette phrase
    dossier = AUDIO / str(lid)
    dossier.mkdir(parents=True, exist_ok=True)
    temporaire = dossier / f"{n}.{extension}.part"
    temporaire.write_bytes(donnees)
    temporaire.replace(dossier / f"{n}.{extension}")  # jamais de fichier à moitié écrit servi au lecteur


def definir_audio(lid, etat, erreur=None):
    """etat=None : plus de livre audio (les fichiers sont effacés). 'attente' relance la génération."""
    if etat is None:
        shutil.rmtree(AUDIO / str(lid), ignore_errors=True)
    faits = len(_fichiers_audio(lid))
    _sql("UPDATE livres SET audio_etat = ?, audio_erreur = ?, audio_faits = ? WHERE id = ?", (etat, erreur, faits, lid))


def lancer_audio(uid, lid, voix_id):
    """La personne demande son livre audio avec cette voix. Changer de voix refait tout le livre.
    Renvoie False si le livre ou la voix n'existe pas (ou si le livre n'est pas à uid)."""
    filtre, p = _proprio(uid)
    lignes, _ = _sql(f"SELECT voix FROM livres WHERE id = ? AND {filtre}", (lid, *p))
    if not lignes or not voix(voix_id):
        return False
    if lignes[0]["voix"] != voix_id:
        definir_audio(lid, None)
        _sql("UPDATE livres SET voix = ? WHERE id = ?", (voix_id, lid))
    definir_audio(lid, "attente")
    return True


def annuler_audio(uid, lid):
    filtre, p = _proprio(uid)
    if not _sql(f"SELECT 1 FROM livres WHERE id = ? AND {filtre}", (lid, *p))[0]:
        return False
    definir_audio(lid, None)
    _sql("UPDATE livres SET voix = NULL WHERE id = ?", (lid,))
    return True


def compter_audio(lid):
    _sql("UPDATE livres SET audio_faits = ? WHERE id = ?", (len(_fichiers_audio(lid)), lid))


def phrases_generees(lid):
    """Numéros des phrases dont l'audio IA est en stock."""
    return {int(f.stem) for f in _fichiers_audio(lid) if f.stem.isdigit()}


def position(lid):
    """Phrase où le propriétaire en est (enregistrée par le lecteur à chaque phrase)."""
    lignes, _ = _sql("SELECT phrase FROM livres WHERE id = ?", (lid,))
    return lignes[0]["phrase"] if lignes else 0


def info_audio(lid):
    lignes, _ = _sql("SELECT voix, audio_etat FROM livres WHERE id = ?", (lid,))
    return lignes[0] if lignes else None


def prochain_travail():
    """Le plus ancien livre en attente de génération (un livre « encours » repris après un redémarrage d'abord)."""
    lignes, _ = _sql("""SELECT id, phrase, voix FROM livres WHERE audio_etat IN ('encours', 'attente')
                        ORDER BY audio_etat = 'encours' DESC, id LIMIT 1""")
    return lignes[0] if lignes else None


def etat_audio(lid):
    lignes, _ = _sql("SELECT audio_etat FROM livres WHERE id = ?", (lid,))
    return lignes[0]["audio_etat"] if lignes else None


def file_audio():
    return _sql("SELECT COUNT(*) n FROM livres WHERE audio_etat IN ('encours', 'attente')")[0][0]["n"]


# ---------- Catalogue de voix (géré par l'admin) ----------
def liste_voix():
    return _sql("SELECT id, nom, instruct, seed, demo IS NOT NULL demo FROM voix ORDER BY id")[0]


def voix(voix_id):
    lignes, _ = _sql("SELECT * FROM voix WHERE id = ?", (voix_id,))
    return lignes[0] if lignes else None


def ajouter_voix(nom, instruct, seed, demo, extension):
    vid = _sql("INSERT INTO voix (nom, instruct, seed) VALUES (?, ?, ?)", (nom, instruct, seed))[1]
    definir_demo(vid, demo, extension)
    return vid


def definir_demo(voix_id, demo, extension):
    (VOIX / f"{voix_id}.{extension}").write_bytes(demo)
    _sql("UPDATE voix SET demo = ? WHERE id = ?", (extension, voix_id))


def fichier_demo(voix_id):
    v = voix(voix_id)
    return VOIX / f"{voix_id}.{v['demo']}" if v and v["demo"] else None


def voix_sans_demo():
    return _sql("SELECT * FROM voix WHERE demo IS NULL")[0]


def supprimer_voix(voix_id):
    """Refuse (False) si des livres attendent encore d'être générés avec cette voix."""
    if _sql("SELECT 1 FROM livres WHERE voix = ? AND audio_etat IN ('attente', 'encours', 'pause')", (voix_id,))[0]:
        return False
    v = voix(voix_id)
    if v and v["demo"]:
        (VOIX / f"{voix_id}.{v['demo']}").unlink(missing_ok=True)
    _sql("UPDATE livres SET voix = NULL WHERE voix = ?", (voix_id,))
    _sql("DELETE FROM voix WHERE id = ?", (voix_id,))
    return True


# ---------- Statistiques ----------
def enregistrer_ecoute(uid, lid, secondes, phrases):
    """Ajoute du temps d'écoute au compteur du jour (le titre est copié : les minutes survivent au livre)."""
    _sql("""INSERT INTO ecoutes (utilisateur, livre, titre, jour, secondes, phrases)
            SELECT ?, id, titre, ?, ?, ? FROM livres WHERE id = ? AND utilisateur = ?
            ON CONFLICT (utilisateur, livre, jour)
            DO UPDATE SET secondes = secondes + excluded.secondes, phrases = phrases + excluded.phrases""",
         (uid, date.today().isoformat(), secondes, phrases, lid, uid))


def stats(uid=None):
    """Statistiques d'un compte, ou de tout le monde si uid vaut None."""
    filtre, p = ("WHERE utilisateur = ?", (uid,)) if uid else ("WHERE 1", ())
    aujourd_hui = date.today()
    jours = [(aujourd_hui - timedelta(days=d)).isoformat() for d in range(29, -1, -1)]
    il_y_a_7j = jours[-7]

    par_jour = {l["jour"]: l["s"] for l in _sql(
        f"SELECT jour, SUM(secondes) s FROM ecoutes {filtre} GROUP BY jour HAVING s > 0", p)[0]}
    total = _sql(f"SELECT COALESCE(SUM(secondes), 0) s, COALESCE(SUM(phrases), 0) p, MIN(jour) depuis FROM ecoutes {filtre}", p)[0][0]
    livres_ = _sql(f"""SELECT COUNT(*) total,
                              COALESCE(SUM(total > 0 AND phrase >= total), 0) termines,
                              COALESCE(SUM(phrase > 0 AND phrase < total), 0) en_cours
                       FROM livres {filtre}""", p)[0][0]
    par_livre = _sql(f"""SELECT titre, SUM(secondes) secondes FROM ecoutes {filtre}
                         GROUP BY titre ORDER BY secondes DESC LIMIT 8""", p)[0]

    # Série : jours consécutifs avec écoute, jusqu'à aujourd'hui (ou hier si rien encore aujourd'hui)
    serie, j = 0, aujourd_hui if aujourd_hui.isoformat() in par_jour else aujourd_hui - timedelta(days=1)
    while j.isoformat() in par_jour:
        serie, j = serie + 1, j - timedelta(days=1)

    return {
        "secondes": total["s"],
        "secondes_7j": sum(s for jour, s in par_jour.items() if jour >= il_y_a_7j),
        "phrases": total["p"],
        "depuis": total["depuis"],
        "jours_actifs": len(par_jour),
        "serie": serie,
        "livres": livres_["total"],
        "termines": livres_["termines"],
        "en_cours": livres_["en_cours"],
        "par_jour": [{"jour": j, "secondes": par_jour.get(j, 0)} for j in jours],
        "par_livre": par_livre,
    }


def stats_par_utilisateur():
    il_y_a_7j = (date.today() - timedelta(days=6)).isoformat()
    return _sql("""SELECT u.id, u.identifiant, u.statut,
               (SELECT COALESCE(SUM(secondes), 0) FROM ecoutes e WHERE e.utilisateur = u.id) secondes,
               (SELECT COALESCE(SUM(secondes), 0) FROM ecoutes e WHERE e.utilisateur = u.id AND jour >= ?) secondes_7j,
               (SELECT COUNT(*) FROM livres l WHERE l.utilisateur = u.id) livres,
               (SELECT COUNT(*) FROM livres l WHERE l.utilisateur = u.id AND l.total > 0 AND l.phrase >= l.total) termines,
               (SELECT MAX(lu_le) FROM livres l WHERE l.utilisateur = u.id) derniere
           FROM utilisateurs u WHERE u.statut = 'valide' ORDER BY secondes DESC, u.identifiant""", (il_y_a_7j,))[0]
