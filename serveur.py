"""Lecteur PDF à voix haute : comptes validés par l'admin, bibliothèque personnelle, voix Google France
en attendant le livre audio généré par l'IA (VoiceStudio, voir travaux.py).

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
import unicodedata
import urllib.request
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from gtts import gTTS

import base
import travaux
from extraction import couverture as couverture_du_pdf, paragraphes

PUBLIC = Path(__file__).parent / "public"
PORT = 8765
MAX_PDF = 50 * 1024 * 1024
MAX_IMAGE = 5 * 1024 * 1024
MAX_JSON = 16 * 1024
# Couvertures : formats reconnus à leurs premiers octets (jamais de SVG, qui peut contenir du script)
IMAGES = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG\r\n\x1a\n": ("png", "image/png")}
TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp3": "audio/mpeg", "wav": "audio/wav"}
ACTIONS_AUDIO = ("generer", "pause", "relancer", "supprimer")
# Couverture cherchée en ligne (Open Library, sans clé) quand le PDF n'a pas d'image de couverture.
# Seul le titre du livre est envoyé. COUVERTURES_EN_LIGNE=0 pour ne jamais rien chercher.
COUVERTURES_EN_LIGNE = os.environ.get("COUVERTURES_EN_LIGNE", "1") == "1"
OPENLIBRARY = "https://openlibrary.org"
OPENLIBRARY_COUVERTURES = "https://covers.openlibrary.org"

# Écoutes en cours, pour l'onglet « En direct » du panel : (compte, livre) -> dernière activité.
# Arrêts demandés par l'admin : (compte, livre) -> instant jusqu'auquel l'audio est refusé (la lecture s'arrête).
# ponytail: en mémoire, remis à zéro au redémarrage ; suffit pour quelques auditeurs simultanés
ECOUTES, ARRETS, verrou_ecoutes = {}, {}, threading.Lock()
ECOUTE_ACTIVE = 45   # secondes sans signe de vie avant qu'une écoute ne soit plus affichée
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


def _mots(texte):
    """Mots significatifs, sans accents ni majuscules (« Le Comte de Monte-Cristo » -> comte, monte, cristo)."""
    texte = unicodedata.normalize("NFKD", texte.lower()).encode("ascii", "ignore").decode()
    return {m for m in re.findall(r"[a-z0-9]+", texte) if len(m) > 2 or m.isdigit()}


def couverture_en_ligne(titre):
    """Image JPEG de la couverture trouvée sur Open Library, ou None si rien ne correspond vraiment :
    tous les mots du titre doivent se retrouver dans le titre ou l'auteur trouvés, et le titre trouvé
    ne doit pas en ajouter plus de 2 (évite « Mon livre » -> « Mon livre de coloriage »)."""
    cherches = _mots(titre)
    if not cherches:
        return None
    entetes = {"User-Agent": "lecteur-pdf (auto-hébergé)"}
    recherche = " ".join(re.findall(r"\w+", titre))       # « george-orwell-1984 » -> « george orwell 1984 »
    url = f"{OPENLIBRARY}/search.json?" + urlencode({"q": recherche, "limit": 5, "fields": "title,author_name,cover_i"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=entetes), timeout=10) as r:
        resultats = json.loads(r.read()).get("docs", [])
    for doc in resultats:
        mots_titre = _mots(doc.get("title", ""))
        mots_auteur = _mots(" ".join(doc.get("author_name", [])))
        if doc.get("cover_i") and cherches <= mots_titre | mots_auteur and len(mots_titre - cherches) <= 2:
            url = f"{OPENLIBRARY_COUVERTURES}/b/id/{int(doc['cover_i'])}-L.jpg?default=false"
            with urllib.request.urlopen(urllib.request.Request(url, headers=entetes), timeout=10) as r:
                image = r.read(MAX_IMAGE + 1)
            return image if image.startswith(b"\xff\xd8\xff") and len(image) <= MAX_IMAGE else None
    return None


def poser_couverture_en_ligne(uid, lid, titre):
    try:
        image = couverture_en_ligne(titre)
    except (OSError, ValueError):  # Open Library injoignable : on garde la couverture générée
        return
    if image and not base.fichier_couverture(None, lid):   # la personne n'en a pas mis une entre-temps
        base.definir_couverture(uid, lid, image, "jpg")


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

    def cible(self):
        """Propriétaire à imposer pour lire ou modifier un livre : soi-même, ou None (tous) pour l'admin."""
        c = self.compte()
        return None if c["admin"] else c["id"]

    def corps(self, limite):
        try:
            taille = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise Erreur(400)
        if taille > limite:
            raise Erreur(413, f"Fichier trop gros ({limite // 1024 // 1024} Mo maximum).")
        return self.rfile.read(taille)

    def fichier(self, chemin):
        if not chemin:
            raise Erreur(404, "Introuvable.")
        self.repondre(chemin.read_bytes(), TYPES[chemin.suffix[1:]])

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
        if not self.path.startswith("/api/"):
            # Pages, scripts et styles : toujours revérifiés (304 si inchangés), sinon après une mise à jour
            # le navigateur peut mélanger une nouvelle page et un ancien commun.js
            self.send_header("Cache-Control", "no-cache")
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
            (r"/api/livres/(\d+)/audio/(\d+)", self.audio_phrase),
            (r"/api/livres/(\d+)/carte", self.carte_audio),
            (r"/api/livres/(\d+)/couverture", lambda lid: self.fichier(base.fichier_couverture(self.cible(), int(lid)))),
            (r"/api/admin/utilisateurs", lambda: self.repondre(base.utilisateurs() if self.admin() else [])),
            (r"/api/admin/livres", lambda: self.repondre(base.tous_les_livres() if self.admin() else [])),
            (r"/api/voix", self.catalogue),
            (r"/api/voix/(\d+)/demo", lambda vid: self.compte() and self.fichier(base.fichier_demo(int(vid)))),
            (r"/api/admin/srv02", lambda: self.repondre(travaux.etat_public() if self.admin() else {})),
            (r"/api/admin/activite", self.admin_activite),
            (r"/api/admin/voix", lambda: self.repondre(base.liste_voix() if self.admin() else [])),
            (r"/api/stats", self.stats),
            (r"/api/admin/stats", lambda: self.repondre(base.stats_par_utilisateur() if self.admin() else [])),
        ])

    def do_POST(self):
        self.traiter([
            (r"/api/inscription", self.inscription),
            (r"/api/connexion", self.connexion),
            (r"/api/deconnexion", self.deconnexion),
            (r"/api/moi/nouveautes", lambda: (base.nouveautes_vues(self.compte()["id"]), self.repondre({"ok": True}))),
            (r"/api/livres", self.importer),
            (r"/api/livres/(\d+)/progression", self.progression),
            (r"/api/livres/(\d+)/couverture", self.couverture),
            (r"/api/livres/(\d+)/audio", self.livre_audio),
            (r"/api/admin/statut", self.statut),
            (r"/api/admin/livres/(\d+)/audio", self.admin_audio),
            (r"/api/admin/livres/(\d+)/progression", self.admin_reinitialiser),
            (r"/api/admin/srv02", self.admin_srv02),
            (r"/api/admin/ecoutes/arreter", self.admin_arreter_ecoute),
            (r"/api/admin/voix", self.admin_ajouter_voix),
            (r"/api/admin/voix/essai", self.admin_essai),
            (r"/api/tts", self.tts),
        ])

    def do_DELETE(self):
        self.traiter([
            (r"/api/livres/(\d+)", self.supprimer),
            (r"/api/livres/(\d+)/couverture", self.retirer_couverture),
            (r"/api/livres/(\d+)/audio", lambda lid: self.repondre({"ok": base.annuler_audio(self.cible(), int(lid))})),
            (r"/api/admin/utilisateurs/(\d+)", self.supprimer_utilisateur),
            (r"/api/admin/utilisateurs/(\d+)/livres", self.admin_vider_bibliotheque),
            (r"/api/admin/utilisateurs/(\d+)/positions", self.admin_oublier_positions),
            (r"/api/admin/voix/(\d+)", self.admin_supprimer_voix),
        ])

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

    def admin_vider_bibliotheque(self, uid):
        self.admin()
        base.supprimer_livres_de(int(uid))
        self.repondre({"ok": True})

    def admin_oublier_positions(self, uid):
        self.admin()
        base.supprimer_positions_gardees(int(uid))
        self.repondre({"ok": True})

    def admin_reinitialiser(self, lid):
        self.admin()
        if not base.livre_visible(None, int(lid)):
            raise Erreur(404, "Livre introuvable.")
        base.reinitialiser_progression(int(lid))
        self.repondre({"ok": True})

    def supprimer_utilisateur(self, uid):
        self.admin()
        if not base.supprimer_utilisateur(int(uid)):
            raise Erreur(404, "Compte introuvable (l'admin ne peut pas être supprimé).")
        self.repondre({"ok": True})

    # ---------- Livres ----------
    def lire_livre(self, lid):
        moi = self.compte()
        l = base.livre(self.cible(), int(lid))
        if not l:
            raise Erreur(404, "Livre introuvable.")
        # L'admin peut écouter le livre de quelqu'un d'autre sans toucher à sa progression ni à ses stats
        l["lecture_seule"] = l["utilisateur"] != moi["id"]
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
        lid, repris = base.ajouter_livre(uid, titre, paras, pdf)
        try:
            image = couverture_du_pdf(pdf)
        except Exception:  # une couverture illisible n'empêche pas l'import
            image = None
        if image:
            base.definir_couverture(uid, lid, image, "jpg")
        elif COUVERTURES_EN_LIGNE:
            threading.Thread(target=poser_couverture_en_ligne, args=(uid, lid, titre), daemon=True).start()
        self.repondre({"id": lid, "repris": repris}, code=201)

    def couverture(self, lid):
        image = self.corps(MAX_IMAGE)
        format_ = next((f for debut, f in IMAGES.items() if image.startswith(debut)), None)
        if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
            format_ = ("webp", "image/webp")
        if not format_:
            raise Erreur(415, "Image JPEG, PNG ou WebP uniquement.")
        if not base.definir_couverture(self.cible(), int(lid), image, format_[0]):
            raise Erreur(404, "Livre introuvable.")
        self.repondre({"ok": True})

    def retirer_couverture(self, lid):
        if not base.definir_couverture(self.cible(), int(lid), None, None):
            raise Erreur(404, "Livre introuvable.")
        self.repondre({"ok": True})

    def progression(self, lid):
        uid = self.compte()["id"]
        d = self.json_recu()
        try:
            phrase, total, vitesse = int(d["phrase"]), int(d["total"]), float(d["vitesse"])
            secondes, lues = float(d.get("secondes", 0)), int(d.get("phrases_lues", 0))
        except (KeyError, ValueError, TypeError):
            raise Erreur(400)
        base.sauver_progression(uid, int(lid), max(0, phrase), max(0, total), min(max(vitesse, 0.5), 5))
        self.noter_ecoute(int(lid), phrase=max(0, phrase))
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
        base.supprimer_livre(self.cible(), int(lid))
        self.repondre({"ok": True})

    def tts(self):
        self.compte()
        d = self.json_recu()
        texte = str(d.get("texte", ""))[:2000]
        if not texte.strip():
            raise Erreur(400)
        if isinstance(d.get("livre"), int):
            self.verifier_arret(d["livre"])
            self.noter_ecoute(d["livre"], voix="google")
        try:
            self.repondre(synthese(texte), "audio/mpeg")
        except Exception:  # Google injoignable ou limite atteinte
            raise Erreur(502, "La voix Google ne répond pas.")

    def carte_audio(self, lid):
        """État du livre audio et carte des phrases déjà en stock, pour le ruban du lecteur."""
        lid = int(lid)
        if not base.livre_visible(self.cible(), lid):
            raise Erreur(404, "Introuvable.")
        info = base.info_audio(lid)
        total = len(base.phrases_a_plat(lid))
        self.repondre({"etat": info["audio_etat"], "voix": info["voix"], "carte": base.carte_audio(lid, total)})

    def audio_phrase(self, lid, n):
        """Audio IA d'une phrase : en stock, sinon généré tout de suite et mis en stock. 404 si l'IA n'est pas
        disponible maintenant (srv-02 endormi) : le lecteur prend alors la voix Google pour cette phrase."""
        lid, n = int(lid), int(n)
        if not base.livre_visible(self.cible(), lid):
            raise Erreur(404, "Introuvable.")
        self.verifier_arret(lid)
        fichier = base.fichier_audio(None, lid, n) or travaux.phrase_a_la_demande(lid, n)
        if fichier:
            self.noter_ecoute(lid, voix="ia")
        self.fichier(fichier)

    # ---------- Écoutes en cours ----------
    def noter_ecoute(self, lid, phrase=None, voix=None):
        c = self.compte()
        with verrou_ecoutes:
            e = ECOUTES.setdefault((c["id"], lid), {"qui": c["identifiant"], "phrase": None, "voix": None})
            e["vu"] = time.time()
            if phrase is not None:
                e["phrase"] = phrase
            if voix:
                e["voix"] = voix

    def verifier_arret(self, lid):
        if ARRETS.get((self.compte()["id"], lid), 0) > time.time():
            raise Erreur(409, "Lecture arrêtée par l'administrateur.")

    # ---------- Livre audio demandé par la personne ----------
    def catalogue(self):
        """Voix proposées (sans leurs réglages internes) et disponibilité des livres audio."""
        self.compte()
        self.repondre({"actif": travaux.actif(),
                       "voix": [{"id": v["id"], "nom": v["nom"], "demo": bool(v["demo"])} for v in base.liste_voix()]})

    def livre_audio(self, lid):
        """La personne lance (ou relance avec une autre voix) la transformation de son livre en livre audio."""
        if not travaux.actif():
            raise Erreur(503, "Les livres audio ne sont pas disponibles sur ce serveur.")
        voix_id = self.json_recu().get("voix")
        if not isinstance(voix_id, int) or not base.lancer_audio(self.cible(), int(lid), voix_id):
            raise Erreur(404, "Livre ou voix introuvable.")
        travaux.nouvelle_demande()
        self.repondre({"ok": True})

    # ---------- Admin : livres audio, srv-02, voix ----------
    def admin_audio(self, lid):
        self.admin()
        action = self.json_recu().get("action")
        if action not in ACTIONS_AUDIO:
            raise Erreur(400)
        l = base.livre(None, int(lid))
        if not l:
            raise Erreur(404, "Livre introuvable.")
        if action == "supprimer":
            base.annuler_audio(None, int(lid))
        elif action == "pause":
            base.definir_audio(int(lid), "pause")
        else:                                               # generer (reprendre) ou relancer (tout refaire)
            if not l["voix"]:
                raise Erreur(400, "Aucune voix choisie pour ce livre : c'est à son propriétaire de la choisir.")
            if action == "relancer":
                base.definir_audio(int(lid), None)          # efface les fichiers déjà générés
            base.definir_audio(int(lid), "attente")
            travaux.nouvelle_demande()
        self.repondre({"ok": True})

    def admin_activite(self):
        """Onglet « En direct » : ce que la GPU génère, la file d'attente, et qui écoute quoi."""
        self.admin()
        maintenant = time.time()
        gen = travaux.etat.get("generation")
        en_generation = None
        if gen and (l := base.resume_livre(gen["livre"])):
            duree = max(maintenant - gen["depuis"], 1)
            en_generation = {**l, "phrase": gen["phrase"], "par_minute": round(gen["faites"] / duree * 60, 1)}
        with verrou_ecoutes:
            for cle in [k for k, e in ECOUTES.items() if maintenant - e["vu"] > ECOUTE_ACTIVE]:
                del ECOUTES[cle]
            ecoutes = [{"utilisateur": uid, "livre": lid, **e} for (uid, lid), e in ECOUTES.items()]
        for e in ecoutes:
            e.update({k: v for k, v in (base.resume_livre(e["livre"]) or {}).items() if k in ("titre", "total")})
            e["il_y_a"] = round(maintenant - e.pop("vu"))
        self.repondre({"generation": en_generation, "file": base.file_detaillee(), "ecoutes": ecoutes})

    def admin_arreter_ecoute(self):
        """Arrête l'écoute de quelqu'un : son lecteur se met en pause à la phrase suivante, avec un message."""
        self.admin()
        d = self.json_recu()
        if not isinstance(d.get("utilisateur"), int) or not isinstance(d.get("livre"), int):
            raise Erreur(400)
        with verrou_ecoutes:
            ARRETS[(d["utilisateur"], d["livre"])] = time.time() + 15
            ECOUTES.pop((d["utilisateur"], d["livre"]), None)
        self.repondre({"ok": True})

    def admin_srv02(self):
        self.admin()
        action = self.json_recu().get("action")
        try:
            if action == "reveiller":
                travaux.reveiller_manuel()
            elif action == "eteindre":
                travaux.eteindre_manuel()
            else:
                raise Erreur(400)
        except (OSError, ValueError, RuntimeError) as e:
            raise Erreur(502, f"srv-02 : {e}")
        self.repondre({"ok": True})

    def reglages_voix(self, d):
        nom = str(d.get("nom", "")).strip()[:60]
        instruct, seed = str(d.get("instruct", "")).strip(), str(d.get("seed", "")).strip()
        if not 0 < len(instruct) <= 200 or not seed.isdigit():
            raise Erreur(400, "Description de voix (200 caractères max.) et graine (nombre) requises.")
        return nom, {"instruct": instruct, "seed": seed}

    def synthese_admin(self, texte, v):
        if not travaux.actif() or not travaux.voicestudio_pret():
            raise Erreur(503, "VoiceStudio ne répond pas : réveille srv-02 d'abord.")
        try:
            return travaux.synthese_ia(texte, v)
        except Exception as e:
            raise Erreur(502, f"VoiceStudio : {e}")

    def admin_essai(self):
        """Écouter une voix avant de l'ajouter au catalogue."""
        self.admin()
        d = self.json_recu()
        _, v = self.reglages_voix(d)
        texte = str(d.get("texte", "")).strip()[:300] or travaux.TEXTE_DEMO
        audio, extension = self.synthese_admin(texte, v)
        self.repondre(audio, TYPES[extension])

    def admin_ajouter_voix(self):
        """Ajoute une voix au catalogue, avec sa démo (générée tout de suite : srv-02 doit être allumé)."""
        self.admin()
        nom, v = self.reglages_voix(self.json_recu())
        if not nom:
            raise Erreur(400, "Donne un nom à la voix.")
        demo, extension = self.synthese_admin(travaux.TEXTE_DEMO, v)
        self.repondre({"id": base.ajouter_voix(nom, v["instruct"], v["seed"], demo, extension)}, code=201)

    def admin_supprimer_voix(self, vid):
        self.admin()
        if not base.supprimer_voix(int(vid)):
            raise Erreur(409, "Des livres attendent encore d'être générés avec cette voix.")
        self.repondre({"ok": True})


def purge_reguliere():
    """Toutes les heures : livres non écoutés depuis 1 mois, positions gardées depuis plus de 2 mois."""
    while True:
        try:
            base.purger()
        except Exception as e:  # ne jamais arrêter la purge pour une erreur passagère (disque, base occupée)
            print(f"Purge : {e}", flush=True)
        time.sleep(3600)

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
    travaux.demarrer()
    threading.Thread(target=purge_reguliere, daemon=True, name="purge").start()
    print(f"Lecteur PDF prêt : http://127.0.0.1:{PORT}" + ("" if travaux.actif() else " (livres audio désactivés : VOICESTUDIO_CLE absente)"))
    ThreadingHTTPServer(("127.0.0.1", PORT), Gestionnaire).serve_forever()
