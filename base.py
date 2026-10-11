"""Base SQLite : comptes, sessions, livres et progression de lecture. Seul module qui touche à la base."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

DONNEES = Path(__file__).parent / "donnees"
BASE = DONNEES / "lecteur.db"
LIVRES = DONNEES / "livres"
DUREE_SESSION = 30 * 86400

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
"""


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
    LIVRES.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(BASE)
    c.executescript(SCHEMA)
    c.close()


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
def livres(uid):
    return _sql("""SELECT id, titre, phrase, total, vitesse, lu_le, ajoute_le FROM livres
                   WHERE utilisateur = ? ORDER BY COALESCE(lu_le, ajoute_le) DESC""", (uid,))[0]


def livre(uid, lid):
    lignes, _ = _sql("SELECT * FROM livres WHERE id = ? AND utilisateur = ?", (lid, uid))
    if not lignes:
        return None
    l = lignes[0]
    l["paragraphes"] = json.loads(l["paragraphes"])
    return l


def ajouter_livre(uid, titre, paragraphes, pdf):
    lid = _sql("INSERT INTO livres (utilisateur, titre, paragraphes, ajoute_le) VALUES (?, ?, ?, ?)",
               (uid, titre, json.dumps(paragraphes, ensure_ascii=False), int(time.time())))[1]
    dossier = LIVRES / str(uid)
    dossier.mkdir(exist_ok=True)
    (dossier / f"{lid}.pdf").write_bytes(pdf)  # gardé pour pouvoir ré-extraire si l'extraction s'améliore
    return lid


def sauver_progression(uid, lid, phrase, total, vitesse):
    _sql("UPDATE livres SET phrase = ?, total = ?, vitesse = ?, lu_le = ? WHERE id = ? AND utilisateur = ?",
         (phrase, total, vitesse, int(time.time()), lid, uid))


def supprimer_livre(uid, lid):
    _sql("DELETE FROM livres WHERE id = ? AND utilisateur = ?", (lid, uid))
    (LIVRES / str(uid) / f"{lid}.pdf").unlink(missing_ok=True)


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
