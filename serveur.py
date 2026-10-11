"""Lecteur PDF à voix haute : comptes validés par l'admin, bibliothèque personnelle, voix Google France.

Lancer :            python serveur.py
Créer l'admin :     python serveur.py --admin <identifiant>
Derrière HTTPS :    définir LECTEUR_HTTPS=1 (cookies Secure)
"""
import io
import json
import os
import re
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from gtts import gTTS

import base
from extraction import paragraphes

PUBLIC = Path(__file__).parent / "public"
PORT = 8765
MAX_PDF = 50 * 1024 * 1024
MAX_JSON = 16 * 1024
SECURE = "; Secure" if os.environ.get("LECTEUR_HTTPS") == "1" else ""
IDENTIFIANT = re.compile(r"[\w.-]{3,32}")

# ponytail: compteur en mémoire par identifiant, remis à zéro au redémarrage ; suffit pour <10 comptes
echecs, verrou = {}, threading.Lock()
MAX_ECHECS, FENETRE = 5, 15 * 60


def trop_d_echecs(identifiant):
    with verrou:
        recents = [t for t in echecs.get(identifiant.lower(), []) if t > time.time() - FENETRE]
        echecs[identifiant.lower()] = recents
        return len(recents) >= MAX_ECHECS


def noter_echec(identifiant):
    with verrou:
        echecs.setdefault(identifiant.lower(), []).append(time.time())


def synthese(texte):
    tampon = io.BytesIO()
    gTTS(texte, lang="fr", tld="fr").write_to_fp(tampon)  # tld="fr" : accent de France
    return tampon.getvalue()


class Erreur(Exception):
    def __init__(self, code, message=""):
        self.code, self.message = code, message


class Gestionnaire(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    # ---------- Outils ----------
    def jeton(self):
        morceau = SimpleCookie(self.headers.get("Cookie", "")).get("session")
        return morceau.value if morceau else None

    def compte(self):
        c = self.jeton() and base.compte_de_session(self.jeton())
        if not c:
            raise Erreur(401, "Connexion requise.")
        return c

    def corps(self, limite):
        try:
            taille = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise Erreur(400)
        if taille > limite:
            raise Erreur(413, "Fichier trop gros (50 Mo maximum).")
        return self.rfile.read(taille)

    def json_recu(self):
        try:
            d = json.loads(self.corps(MAX_JSON))
        except ValueError:
            raise Erreur(400)
        if not isinstance(d, dict):
            raise Erreur(400)
        return d

    def repondre(self, corps, type_mime="application/json", code=200, entetes=()):
        if not isinstance(corps, bytes):
            corps = json.dumps(corps, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        for cle, valeur in entetes:
            self.send_header(cle, valeur)
        self.end_headers()
        self.wfile.write(corps)

    def traiter(self, routes):
        for motif, action in routes:
            m = re.fullmatch(motif, self.path.split("?")[0])
            if m:
                try:
                    return action(*m.groups())
                except Erreur as e:
                    return self.repondre({"erreur": e.message}, code=e.code)
        self.repondre({"erreur": "Introuvable."}, code=404)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def list_directory(self, path):  # pas de listing de dossier
        self.send_error(404)

    def log_message(self, *_):
        pass

    # ---------- Méthodes HTTP ----------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/bibliotheque.html")
            return self.end_headers()
        if not self.path.startswith("/api/"):
            return super().do_GET()
        self.traiter([
            (r"/api/moi", lambda: self.repondre(self.compte())),
            (r"/api/livres", lambda: self.repondre(base.livres(self.compte()["id"]))),
            (r"/api/livres/(\d+)", self.lire_livre),
            (r"/api/admin/utilisateurs", lambda: self.repondre(base.utilisateurs() if self.admin() else [])),
            (r"/api/stats", self.stats),
            (r"/api/admin/stats", lambda: self.repondre(base.stats_par_utilisateur() if self.admin() else [])),
        ])

    def do_POST(self):
        self.traiter([
            (r"/api/inscription", self.inscription),
            (r"/api/connexion", self.connexion),
            (r"/api/deconnexion", self.deconnexion),
            (r"/api/livres", self.importer),
            (r"/api/livres/(\d+)/progression", self.progression),
            (r"/api/admin/statut", self.statut),
            (r"/api/tts", self.tts),
        ])

    def do_DELETE(self):
        self.traiter([(r"/api/livres/(\d+)", self.supprimer)])

    # ---------- Comptes ----------
    def identifiants(self):
        d = self.json_recu()
        identifiant, mdp = str(d.get("identifiant", "")).strip(), str(d.get("mdp", ""))
        if not IDENTIFIANT.fullmatch(identifiant):
            raise Erreur(400, "Identifiant : 3 à 32 caractères (lettres, chiffres, . - _).")
        if not 8 <= len(mdp) <= 256:
            raise Erreur(400, "Mot de passe : 8 caractères minimum.")
        return identifiant, mdp

    def inscription(self):
        identifiant, mdp = self.identifiants()
        if base.creer_utilisateur(identifiant, mdp) is None:
            raise Erreur(409, "Cet identifiant est déjà pris.")
        self.repondre({"ok": True}, code=201)

    def connexion(self):
        identifiant, mdp = self.identifiants()
        if trop_d_echecs(identifiant):
            raise Erreur(429, "Trop d'essais. Réessaie dans 15 minutes.")
        c = base.authentifier(identifiant, mdp)
        if not c:
            noter_echec(identifiant)
            raise Erreur(401, "Identifiant ou mot de passe incorrect.")
        if c["statut"] == "en_attente":
            raise Erreur(403, "Ton compte attend la validation de l'administrateur.")
        if c["statut"] == "refuse":
            raise Erreur(403, "Ce compte n'a pas été accepté.")
        cookie = (f"session={base.ouvrir_session(c['id'])}; Path=/; Max-Age={base.DUREE_SESSION}; "
                  f"HttpOnly; SameSite=Lax{SECURE}")
        self.repondre({"ok": True}, entetes=[("Set-Cookie", cookie)])

    def deconnexion(self):
        if self.jeton():
            base.fermer_session(self.jeton())
        self.repondre({"ok": True}, entetes=[("Set-Cookie", f"session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{SECURE}")])

    def admin(self):
        if not self.compte()["admin"]:
            raise Erreur(403, "Réservé à l'administrateur.")
        return True

    def statut(self):
        self.admin()
        d = self.json_recu()
        if d.get("statut") not in ("valide", "refuse") or not isinstance(d.get("id"), int):
            raise Erreur(400)
        base.definir_statut(d["id"], d["statut"])
        self.repondre({"ok": True})

    # ---------- Livres ----------
    def lire_livre(self, lid):
        l = base.livre(self.compte()["id"], int(lid))
        if not l:
            raise Erreur(404, "Livre introuvable.")
        self.repondre(l)

    def importer(self):
        uid = self.compte()["id"]
        pdf = self.corps(MAX_PDF)
        try:
            paras = paragraphes(pdf)
        except Exception:  # PDF illisible ou protégé
            raise Erreur(422, "Ce PDF est illisible ou protégé.")
        if not paras:
            raise Erreur(422, "Aucun texte trouvé : ce PDF est probablement une image scannée.")
        titre = unquote(self.headers.get("X-Nom", "")).strip()
        titre = re.sub(r"\.pdf$", "", titre, flags=re.I)[:200] or "Sans titre"
        self.repondre({"id": base.ajouter_livre(uid, titre, paras, pdf)}, code=201)

    def progression(self, lid):
        uid = self.compte()["id"]
        d = self.json_recu()
        try:
            phrase, total, vitesse = int(d["phrase"]), int(d["total"]), float(d["vitesse"])
            secondes, lues = float(d.get("secondes", 0)), int(d.get("phrases_lues", 0))
        except (KeyError, ValueError, TypeError):
            raise Erreur(400)
        base.sauver_progression(uid, int(lid), max(0, phrase), max(0, total), min(max(vitesse, 0.5), 5))
        # Plafonds : le lecteur envoie à chaque phrase, un envoi ne peut pas couvrir plus de 15 min
        secondes, lues = min(max(secondes, 0), 900), min(max(lues, 0), 500)
        if secondes > 0 or lues > 0:
            base.enregistrer_ecoute(uid, int(lid), secondes, lues)
        self.repondre({"ok": True})

    def stats(self):
        moi = self.compte()
        cible = parse_qs(urlsplit(self.path).query).get("utilisateur", [str(moi["id"])])[0]
        if cible != str(moi["id"]):
            self.admin()
        if cible != "tous" and not cible.isdigit():
            raise Erreur(400)
        self.repondre(base.stats(None if cible == "tous" else int(cible)))

    def supprimer(self, lid):
        base.supprimer_livre(self.compte()["id"], int(lid))
        self.repondre({"ok": True})

    def tts(self):
        self.compte()
        texte = str(self.json_recu().get("texte", ""))[:2000]
        if not texte.strip():
            raise Erreur(400)
        try:
            self.repondre(synthese(texte), "audio/mpeg")
        except Exception:  # Google injoignable ou limite atteinte
            raise Erreur(502, "La voix Google ne répond pas.")


if __name__ == "__main__":
    import sys
    base.initialiser()
    if len(sys.argv) == 3 and sys.argv[1] == "--admin":
        from getpass import getpass
        mdp = getpass("Mot de passe admin (8 caractères min.) : ")
        if len(mdp) < 8 or not IDENTIFIANT.fullmatch(sys.argv[2]):
            sys.exit("Identifiant ou mot de passe invalide.")
        base.promouvoir_admin(sys.argv[2], mdp)
        sys.exit(f"Admin « {sys.argv[2]} » prêt.")
    print(f"Lecteur PDF prêt : http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Gestionnaire).serve_forever()
