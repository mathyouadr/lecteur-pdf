"""Test de bout en bout de l'API, sur une base jetable. Lancer : python test_serveur.py"""
import json
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf

import base
import serveur
import travaux

# Découpage en phrases (fait côté serveur : il fixe le numéro de phrase de la progression et de l'audio)
from extraction import phrases
assert phrases("M. Smith arriva. Il dit : « Bonjour ! » Puis il partit… Vraiment ?") == \
    ["M. Smith arriva.", "Il dit : « Bonjour ! »", "Puis il partit…", "Vraiment ?"]
assert phrases("— Oui. — Non, dit J. Orwell en 1949. 1984 parut ensuite.") == \
    ["— Oui.", "— Non, dit J. Orwell en 1949.", "1984 parut ensuite."]
assert phrases("Une seule phrase sans point final") == ["Une seule phrase sans point final"]
assert phrases("etc. et ainsi de suite. Fin.") == ["etc. et ainsi de suite.", "Fin."]

tmp = Path(tempfile.mkdtemp())
base.DONNEES, base.BASE, base.LIVRES = tmp, tmp / "test.db", tmp / "livres"
base.AUDIO, base.COUVERTURES, base.VOIX = tmp / "audio", tmp / "couvertures", tmp / "voix"
serveur.COUVERTURES_EN_LIGNE = False       # pas d'Open Library pendant les tests (sauf avec le faux, plus bas)
base.initialiser()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), serveur.Gestionnaire)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{httpd.server_port}"


def appel(chemin, donnees=None, cookie=None, methode=None, brut=None, entetes=None):
    corps = brut if brut is not None else (json.dumps(donnees).encode() if donnees is not None else None)
    req = urllib.request.Request(URL + chemin, data=corps, method=methode, headers=entetes or {})
    if donnees is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def connexion(identifiant, mdp):
    code, _, h = appel("/api/connexion", {"identifiant": identifiant, "mdp": mdp})
    return code, (h.get("Set-Cookie", "").split(";")[0] or None)


# Fichiers sensibles jamais servis
assert appel("/test.db")[0] == 404 and appel("/../base.py")[0] == 404 and appel("/serveur.py")[0] == 404
assert appel("/bibliotheque.html")[0] == 200
assert appel("/commun.js")[2]["Cache-Control"] == "no-cache"     # jamais un ancien script après une mise à jour

# Inscription -> en attente -> refus de connexion
assert appel("/api/inscription", {"identifiant": "alice", "mdp": "motdepasse1"})[0] == 201
assert appel("/api/inscription", {"identifiant": "ALICE", "mdp": "motdepasse1"})[0] == 409
assert appel("/api/inscription", {"identifiant": "a", "mdp": "court"})[0] == 400
assert connexion("alice", "motdepasse1")[0] == 403

# Admin valide Alice
base.promouvoir_admin("admin", "secretadmin")
code, admin = connexion("admin", "secretadmin")
assert code == 200 and admin
alice_id = next(u["id"] for u in json.loads(appel("/api/admin/utilisateurs", cookie=admin)[1]) if u["identifiant"] == "alice")
assert appel("/api/admin/statut", {"id": alice_id, "statut": "valide"}, cookie=admin)[0] == 200

code, alice = connexion("alice", "motdepasse1")
assert code == 200 and "HttpOnly" in appel("/api/connexion", {"identifiant": "alice", "mdp": "motdepasse1"})[2]["Set-Cookie"]
assert appel("/api/admin/utilisateurs", cookie=alice)[0] == 403
assert appel("/api/livres")[0] == 401
assert appel("/api/tts", {"texte": "x"})[0] == 401

# Import d'un PDF, lecture, progression
doc = pymupdf.open()
doc.new_page().insert_text((72, 72), "Bonjour le monde. Deuxième phrase ici.")
code, corps, _ = appel("/api/livres", cookie=alice, brut=doc.tobytes(),
                       entetes={"Content-Type": "application/pdf", "X-Nom": "Mon%20livre.pdf"})
assert code == 201, corps
lid = json.loads(corps)["id"]
livre = json.loads(appel(f"/api/livres/{lid}", cookie=alice)[1])
assert livre["titre"] == "Mon livre" and livre["paragraphes"] == [["Bonjour le monde.", "Deuxième phrase ici."]]
assert livre["total"] == 2 and livre["lecture_seule"] is False and livre["audio_etat"] is None  # pas de VoiceStudio configuré
assert appel("/api/livres", cookie=alice, brut=b"pas un pdf", entetes={"Content-Type": "application/pdf"})[0] == 422
assert appel(f"/api/livres/{lid}/progression", {"phrase": 1, "total": 2, "vitesse": 9}, cookie=alice)[0] == 200
l = json.loads(appel("/api/livres", cookie=alice)[1])[0]
assert (l["phrase"], l["total"], l["vitesse"]) == (1, 2, 5) and l["lu_le"]

# Temps d'écoute et statistiques
assert appel(f"/api/livres/{lid}/progression", {"phrase": 2, "total": 2, "vitesse": 1, "secondes": 90, "phrases_lues": 3}, cookie=alice)[0] == 200
assert appel(f"/api/livres/{lid}/progression", {"phrase": 2, "total": 2, "vitesse": 1, "secondes": 99999, "phrases_lues": 1}, cookie=alice)[0] == 200
s = json.loads(appel("/api/stats", cookie=alice)[1])
assert s["secondes"] == 990 and s["phrases"] == 4 and s["livres"] == 1 and s["termines"] == 1 and s["serie"] == 1, s
assert len(s["par_jour"]) == 30 and s["par_jour"][-1]["secondes"] == 990 and s["par_livre"][0]["titre"] == "Mon livre"
assert appel(f"/api/stats?utilisateur={alice_id}", cookie=alice)[0] == 200
assert appel("/api/stats?utilisateur=tous", cookie=alice)[0] == 403          # pas les stats des autres
admin_id = json.loads(appel("/api/moi", cookie=admin)[1])["id"]
assert appel(f"/api/admin/utilisateurs/{admin_id}", cookie=admin, methode="DELETE")[0] == 404   # jamais l'admin
assert appel(f"/api/stats?utilisateur={admin_id}", cookie=alice)[0] == 403
assert appel("/api/admin/stats", cookie=alice)[0] == 403
assert json.loads(appel("/api/stats?utilisateur=tous", cookie=admin)[1])["secondes"] == 990
assert json.loads(appel(f"/api/stats?utilisateur={alice_id}", cookie=admin)[1])["secondes"] == 990
lignes = {l["identifiant"]: l for l in json.loads(appel("/api/admin/stats", cookie=admin)[1])}
assert lignes["alice"]["secondes"] == 990 and lignes["alice"]["termines"] == 1 and lignes["admin"]["secondes"] == 0
assert appel("/api/stats?utilisateur=abc", cookie=admin)[0] == 400

# L'admin peut ouvrir le livre d'Alice, en lecture seule : ni sa progression ni ses stats ne bougent
vu = json.loads(appel(f"/api/livres/{lid}", cookie=admin)[1])
assert vu["lecture_seule"] is True and vu["proprietaire"] == "alice"
assert json.loads(appel("/api/livres", cookie=admin)[1]) == []                   # sa propre bibliothèque reste vide
appel(f"/api/livres/{lid}/progression", {"phrase": 0, "total": 2, "vitesse": 1, "secondes": 50}, cookie=admin)
assert json.loads(appel("/api/livres", cookie=alice)[1])[0]["phrase"] == 2
assert json.loads(appel("/api/stats", cookie=alice)[1])["secondes"] == 990
assert [l["titre"] for l in json.loads(appel("/api/admin/livres", cookie=admin)[1])] == ["Mon livre"]
assert appel("/api/admin/livres", cookie=alice)[0] == 403

# Un autre compte ne voit ni le livre, ni sa couverture, ni son audio
base.creer_utilisateur("bob", "motdepasse2", "valide")
_, bob = connexion("bob", "motdepasse2")
assert appel(f"/api/livres/{lid}", cookie=bob)[0] == 404
appel(f"/api/livres/{lid}", cookie=bob, methode="DELETE")
assert appel(f"/api/livres/{lid}", cookie=alice)[0] == 200

# Couverture importée : JPEG, PNG ou WebP reconnus à leurs premiers octets, rien d'autre
png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
assert appel(f"/api/livres/{lid}/couverture", cookie=alice, brut=b"<svg onload=alert(1)>",
             entetes={"Content-Type": "image/svg+xml"})[0] == 415
assert appel(f"/api/livres/{lid}/couverture", cookie=bob, brut=png, entetes={"Content-Type": "image/png"})[0] == 404
assert appel(f"/api/livres/{lid}/couverture", cookie=alice, brut=png, entetes={"Content-Type": "image/png"})[0] == 200
code, corps, h = appel(f"/api/livres/{lid}/couverture", cookie=alice)
assert code == 200 and corps == png and h["Content-Type"] == "image/png"
assert appel(f"/api/livres/{lid}/couverture", cookie=bob)[0] == 404
assert appel(f"/api/livres/{lid}/couverture", cookie=admin)[0] == 200
assert json.loads(appel("/api/livres", cookie=alice)[1])[0]["couverture"] == "png"
assert appel(f"/api/livres/{lid}/couverture", cookie=alice, methode="DELETE")[0] == 200
assert appel(f"/api/livres/{lid}/couverture", cookie=alice)[0] == 404 and not list(base.COUVERTURES.iterdir())

# Couverture automatique : un PDF qui commence par une grande image la prend comme couverture
illustre = pymupdf.open()
page = illustre.new_page()
pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 300), False)
pix.set_rect(pix.irect, (200, 40, 90))
page.insert_image(page.rect, stream=pix.tobytes("png"))
illustre.new_page().insert_text((72, 72), "Le texte commence ici. Deuxième phrase.")
code, corps, _ = appel("/api/livres", cookie=alice, brut=illustre.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "Illustre.pdf"})
lid_illustre = json.loads(corps)["id"]
code, corps, h = appel(f"/api/livres/{lid_illustre}/couverture", cookie=alice)
assert code == 200 and h["Content-Type"] == "image/jpeg" and corps[:3] == b"\xff\xd8\xff"
assert base.livre(None, lid)["couverture"] is None                   # « Mon livre » commence par du texte
assert appel(f"/api/livres/{lid_illustre}", cookie=alice, methode="DELETE")[0] == 200

# Sinon, couverture cherchée sur Open Library (faux serveur), seulement si le titre correspond vraiment
jpeg = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 30), False).tobytes("jpg")
DOCS = {"germinal": [{"title": "Germinal", "author_name": ["Émile Zola"], "cover_i": 42}],
        "mon livre": [{"title": "Mon livre de coloriage pour les petits", "author_name": ["X"], "cover_i": 43},
                      {"title": "Mon livre", "author_name": ["Y"]}]}


class FauxOpenLibrary(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/search.json"):
            q = parse_qs(urlsplit(self.path).query)["q"][0].lower()
            corps, type_ = json.dumps({"docs": DOCS.get(q, [])}).encode(), "application/json"
        else:
            corps, type_ = jpeg, "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", type_)
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *_):
        pass


ol = ThreadingHTTPServer(("127.0.0.1", 0), FauxOpenLibrary)
threading.Thread(target=ol.serve_forever, daemon=True).start()
serveur.OPENLIBRARY = serveur.OPENLIBRARY_COUVERTURES = f"http://127.0.0.1:{ol.server_port}"
assert serveur.couverture_en_ligne("Germinal") == jpeg
assert serveur.couverture_en_ligne("Mon livre") is None          # titre trop différent, ou pas de couverture
assert serveur.couverture_en_ligne("Inconnu au bataillon") is None
serveur.COUVERTURES_EN_LIGNE = True
code, corps, _ = appel("/api/livres", cookie=alice, brut=doc.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "Germinal.pdf"})
lid_g = json.loads(corps)["id"]
for _ in range(50):                                               # cherchée en arrière-plan, après l'import
    if base.fichier_couverture(None, lid_g):
        break
    time.sleep(0.1)
assert base.fichier_couverture(None, lid_g).read_bytes() == jpeg
serveur.COUVERTURES_EN_LIGNE = False
ol.shutdown()
assert appel(f"/api/livres/{lid_g}", cookie=alice, methode="DELETE")[0] == 200

# Audio IA d'une phrase : 404 tant qu'il n'est pas généré (le lecteur passe alors sur la voix Google)
assert appel(f"/api/livres/{lid}/audio/0", cookie=alice)[0] == 404
base.ecrire_audio(lid, 0, b"RIFF....WAVE", "wav")
code, corps, h = appel(f"/api/livres/{lid}/audio/0", cookie=alice)
assert code == 200 and corps == b"RIFF....WAVE" and h["Content-Type"] == "audio/wav"
assert appel(f"/api/livres/{lid}/audio/0", cookie=bob)[0] == 404
assert appel(f"/api/livres/{lid}/audio/0", cookie=admin)[0] == 200

# Catalogue de voix : visible par tout compte connecté, sans les réglages internes
cat = json.loads(appel("/api/voix", cookie=alice)[1])
assert cat["actif"] is False and [v["nom"] for v in cat["voix"]] == [n for n, _, _ in base.VOIX_DEPART]
assert not cat["voix"][0]["demo"] and "instruct" not in cat["voix"][0]
v1, v2 = cat["voix"][0]["id"], cat["voix"][1]["id"]
assert appel("/api/voix")[0] == 401
assert base.etat_audio(lid) is None                                                  # rien n'est lancé à l'import
assert appel(f"/api/livres/{lid}/audio", {"voix": v1}, cookie=alice)[0] == 503       # VoiceStudio pas configuré

# File de génération, contre un faux VoiceStudio
recu = []


class FauxVoiceStudio(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def do_POST(self):
        recu.append((self.headers["Authorization"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.end_headers()
        self.wfile.write(b"RIFF-faux-wav")

    def log_message(self, *_):
        pass


faux = ThreadingHTTPServer(("127.0.0.1", 0), FauxVoiceStudio)
threading.Thread(target=faux.serve_forever, daemon=True).start()
travaux.VOICESTUDIO_URL, travaux.VOICESTUDIO_CLE = f"http://127.0.0.1:{faux.server_port}", "cle-test"
travaux.shutil.which = lambda _: None      # pas de ffmpeg : les fichiers restent en WAV

# C'est la personne qui lance son livre audio, avec la voix de son choix
assert appel(f"/api/livres/{lid}/audio", {"voix": 999}, cookie=alice)[0] == 404
assert appel(f"/api/livres/{lid}/audio", {"voix": "1"}, cookie=alice)[0] == 404
assert appel(f"/api/livres/{lid}/audio", {"voix": v1}, cookie=bob)[0] == 404
assert appel(f"/api/livres/{lid}/audio", {"voix": v1}, cookie=alice)[0] == 200
assert base.etat_audio(lid) == "attente" and base.fichier_audio(None, lid, 0) is None  # nouvelle voix : on repart de zéro

# Pilotage du livre audio par l'admin
assert appel(f"/api/admin/livres/{lid}/audio", {"action": "generer"}, cookie=alice)[0] == 403
assert appel(f"/api/admin/livres/{lid}/audio", {"action": "autre"}, cookie=admin)[0] == 400
assert appel(f"/api/admin/livres/{lid}/audio", {"action": "pause"}, cookie=admin)[0] == 200
assert base.etat_audio(lid) == "pause" and base.prochain_travail() is None
assert appel(f"/api/admin/livres/{lid}/audio", {"action": "generer"}, cookie=admin)[0] == 200
assert base.etat_audio(lid) == "attente"

# Un tour de file : d'abord les démos manquantes du catalogue, puis le livre, avec la voix choisie
assert travaux._tour() is True
assert base.etat_audio(lid) == "pret" and len(recu) == len(base.VOIX_DEPART) + 2
assert all(cle == "Bearer cle-test" and "French" in corps for cle, corps in recu)
assert sum("Deuxième phrase ici." in c and base.VOIX_DEPART[0][1] in c for _, c in recu) == 1
assert all(v["demo"] for v in json.loads(appel("/api/voix", cookie=alice)[1])["voix"])
code, corps, h = appel(f"/api/voix/{v1}/demo", cookie=bob)
assert code == 200 and h["Content-Type"] == "audio/wav" and appel(f"/api/voix/{v1}/demo")[0] == 401
assert appel(f"/api/livres/{lid}/audio/1", cookie=alice)[0] == 200
l = json.loads(appel("/api/livres", cookie=alice)[1])[0]
assert (l["audio_etat"], l["audio_faits"], l["voix"]) == ("pret", 2, v1)

# Changer de voix refait tout le livre
assert appel(f"/api/livres/{lid}/audio", {"voix": v2}, cookie=alice)[0] == 200
assert base.fichier_audio(None, lid, 0) is None and travaux._tour() is True
assert base.etat_audio(lid) == "pret" and sum(base.VOIX_DEPART[1][1] in c for _, c in recu[-2:]) == 2
s2 = json.loads(appel("/api/admin/srv02", cookie=admin)[1])
assert s2["configure"] and s2["voicestudio"] and s2["file"] == 0
assert appel("/api/admin/srv02", {"action": "eteindre"}, cookie=admin)[0] == 502   # pas de jeton Proxmox ici

# Catalogue géré par l'admin : essai, ajout (avec sa démo), suppression
assert appel("/api/admin/voix", cookie=alice)[0] == 403
assert appel("/api/admin/voix", {"nom": "X", "instruct": "male", "seed": "1"}, cookie=alice)[0] == 403
assert appel("/api/admin/voix/essai", {"instruct": "female, child", "seed": "abc"}, cookie=admin)[0] == 400
assert appel("/api/admin/voix/essai", {"instruct": "female, child", "seed": "3"}, cookie=admin)[0] == 200
assert appel("/api/admin/voix", {"nom": "", "instruct": "female, child", "seed": "3"}, cookie=admin)[0] == 400
code, corps, _ = appel("/api/admin/voix", {"nom": "Enfant", "instruct": "female, child", "seed": "3"}, cookie=admin)
assert code == 201
v3 = json.loads(corps)["id"]
assert base.fichier_demo(v3) and len(json.loads(appel("/api/admin/voix", cookie=admin)[1])) == len(base.VOIX_DEPART) + 1

# Un nouvel import n'est pas transformé tant que la personne ne l'a pas demandé
code, corps, _ = appel("/api/livres", cookie=bob, brut=doc.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "B.pdf"})
lid_bob = json.loads(corps)["id"]
assert base.etat_audio(lid_bob) is None and base.file_audio() == 0
assert appel(f"/api/livres/{lid_bob}/audio", {"voix": v3}, cookie=bob)[0] == 200
assert appel(f"/api/admin/voix/{v3}", cookie=admin, methode="DELETE")[0] == 409   # un livre l'attend encore
assert travaux._tour() is True and base.fichier_audio(None, lid_bob, 0)
assert appel(f"/api/admin/voix/{v3}", cookie=admin, methode="DELETE")[0] == 200
assert json.loads(appel(f"/api/livres/{lid_bob}", cookie=bob)[1])["voix"] is None   # l'audio déjà fait reste
assert appel(f"/api/livres/{lid_bob}/audio", cookie=bob, methode="DELETE")[0] == 200
assert base.etat_audio(lid_bob) is None and base.fichier_audio(None, lid_bob, 0) is None
assert appel(f"/api/livres/{lid_bob}/audio", {"voix": v1}, cookie=bob)[0] == 200
assert travaux._tour() is True and base.fichier_audio(None, lid_bob, 0)

# Écouter pendant la génération : une phrase pas encore prête est générée à la demande et mise en stock
long = pymupdf.open()
long.new_page().insert_text((72, 72), "Un. Deux. Trois. Quatre. Cinq. Six. Sept. Huit.")
code, corps, _ = appel("/api/livres", cookie=bob, brut=long.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "Long.pdf"})
lid_long = json.loads(corps)["id"]
proprio_long = base.livre(None, lid_long)["utilisateur"]
assert base.livre(None, lid_long)["total"] == 8
assert appel(f"/api/livres/{lid_long}/audio/3", cookie=bob)[0] == 404       # pas de livre audio demandé : voix Google
assert appel(f"/api/livres/{lid_long}/audio", {"voix": v1}, cookie=bob)[0] == 200
avant = len(recu)
code, corps, _ = appel(f"/api/livres/{lid_long}/audio/3", cookie=bob)
assert code == 200 and base.fichier_audio(None, lid_long, 3) and len(recu) == avant + 1 and "Quatre." in recu[-1][1]
assert appel(f"/api/livres/{lid_long}/audio/3", cookie=bob)[0] == 200 and len(recu) == avant + 1   # servi du stock
assert appel(f"/api/livres/{lid_long}/audio/99", cookie=bob)[0] == 404
assert appel(f"/api/livres/{lid_long}/audio/3", cookie=alice)[0] == 404     # pas son livre

# La file suit l'auditeur : il saute à la phrase 6 pendant la génération, elle repart de là
ordre, original = [], travaux.synthese_ia


def suivi(texte, v, delai=300):
    ordre.append(texte)
    if len(ordre) == 1:
        base.sauver_progression(proprio_long, lid_long, 5, 8, 1)
    return original(texte, v, delai)


travaux.synthese_ia = suivi
assert travaux._tour() is True
travaux.synthese_ia = original
assert ordre == ["Un.", "Six.", "Sept.", "Huit.", "Deux.", "Trois.", "Cinq."], ordre   # « Quatre. » était en stock
assert base.etat_audio(lid_long) == "pret" and base.phrases_generees(lid_long) == set(range(8))

# Pendant qu'un auditeur génère à la demande, la file lui laisse la GPU, puis reprend seule
assert appel(f"/api/livres/{lid_long}/audio", cookie=bob, methode="DELETE")[0] == 200
assert appel(f"/api/livres/{lid_long}/audio", {"voix": v1}, cookie=bob)[0] == 200
travaux.PRIORITE_ECOUTE = 1
avant = len(recu)
assert appel(f"/api/livres/{lid_long}/audio/7", cookie=bob)[0] == 200 and len(recu) == avant + 1
file = threading.Thread(target=travaux._tour)
file.start()
time.sleep(0.6)
assert len(recu) == avant + 1, "la file a pris la GPU pendant l'écoute"
assert "écoute" in json.loads(appel("/api/admin/srv02", cookie=admin)[1])["message"]
file.join(timeout=10)
assert base.etat_audio(lid_long) == "pret" and len(recu) == avant + 8           # reprise après le délai
travaux.PRIORITE_ECOUTE = 8

# Onglet « En direct » : génération en cours, file d'attente, écoutes, et arrêt d'une écoute
assert appel("/api/admin/activite", cookie=bob)[0] == 403
assert travaux.etat["generation"] is None                                      # la file ne génère plus rien
appel(f"/api/livres/{lid_long}/progression", {"phrase": 3, "total": 8, "vitesse": 1}, cookie=bob)
assert appel(f"/api/livres/{lid_long}/audio/3", cookie=bob)[0] == 200
act = json.loads(appel("/api/admin/activite", cookie=admin)[1])
assert act["generation"] is None and act["file"] == []
e = next(x for x in act["ecoutes"] if x["qui"] == "bob")
assert (e["qui"], e["livre"], e["phrase"], e["voix"], e["titre"]) == ("bob", lid_long, 3, "ia", "Long.pdf".removesuffix(".pdf"))
assert appel("/api/admin/ecoutes/arreter", {"utilisateur": proprio_long, "livre": lid_long}, cookie=bob)[0] == 403
assert appel("/api/admin/ecoutes/arreter", {"utilisateur": "x", "livre": lid_long}, cookie=admin)[0] == 400
assert appel("/api/admin/ecoutes/arreter", {"utilisateur": proprio_long, "livre": lid_long}, cookie=admin)[0] == 200
code, corps, _ = appel(f"/api/livres/{lid_long}/audio/3", cookie=bob)
assert code == 409 and "arrêtée" in json.loads(corps)["erreur"]                 # le lecteur se met en pause
assert appel("/api/tts", {"texte": "x", "livre": lid_long}, cookie=bob)[0] == 409
assert appel(f"/api/livres/{lid_long}/audio/3", cookie=admin)[0] == 200         # seule son écoute est arrêtée
assert all(x["qui"] != "bob" for x in json.loads(appel("/api/admin/activite", cookie=admin)[1])["ecoutes"])
serveur.ARRETS.clear()

# L'admin lance lui-même le livre audio d'un livre qui n'en a pas, avec la voix de son choix
code, corps, _ = appel("/api/livres", cookie=bob, brut=long.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "Sans audio.pdf"})
lid_sans = json.loads(corps)["id"]
assert base.etat_audio(lid_sans) is None
assert appel(f"/api/livres/{lid_sans}/audio", {"voix": v2}, cookie=admin)[0] == 200
file = json.loads(appel("/api/admin/activite", cookie=admin)[1])["file"]
assert [(l["id"], l["audio_etat"], l["voix"], l["identifiant"]) for l in file] == [(lid_sans, "attente", v2, "bob")]
assert appel(f"/api/livres/{lid_sans}/audio", cookie=admin, methode="DELETE")[0] == 200
faux.shutdown()

# srv-02 éteint : pas d'audio IA à la demande, réponse rapide pour que le lecteur passe vite sur Google
assert appel(f"/api/livres/{lid_long}/audio", {"voix": v2}, cookie=bob)[0] == 200
travaux.etat.update(vs_vu=0.0)
debut = time.time()
assert appel(f"/api/livres/{lid_long}/audio/0", cookie=bob)[0] == 404 and time.time() - debut < 4
debut = time.time()
assert appel(f"/api/livres/{lid_long}/audio/1", cookie=bob)[0] == 404 and time.time() - debut < 1   # état mémorisé
assert appel(f"/api/livres/{lid_long}/audio", cookie=bob, methode="DELETE")[0] == 200

# Réveil et extinction de srv-02 (paquet Wake-on-LAN et API Proxmox simulés)
reveils, extinctions = [], []
travaux.reveiller = lambda: (reveils.append(1), travaux.etat.update(reveil_envoye=travaux.time.time()))
travaux.eteindre = lambda: extinctions.append(1)
travaux.SRV02_MAC, travaux.PVE_JETON, travaux.DELAI_EXTINCTION = "8c:89:a5:c2:03:02", "jeton-test", 0
travaux.etat.update(eteint_manuel=True, reveil_envoye=0.0)
base.definir_audio(lid_bob, "attente")                       # du travail, mais VoiceStudio ne répond plus
assert travaux._tour() is False and reveils == []            # éteint à la main : la file ne le rallume pas
travaux.nouvelle_demande()
assert travaux._tour() is False and reveils == [1] and travaux.etat["reveil_auto"]   # une demande relance le réveil
assert travaux._tour() is False and reveils == [1]           # pas un paquet toutes les 15 s
base.definir_audio(lid_bob, "pret")
travaux._tour()                                               # plus de travail : compte à rebours de 0 s...
travaux._tour()
assert extinctions == [1] and not travaux.etat["reveil_auto"]  # ...puis extinction, une seule fois
travaux._tour()
assert extinctions == [1]
travaux.etat.update(manuel=True, reveil_auto=True)           # allumé à la main : jamais d'extinction auto
travaux._tour()
travaux._tour()
assert extinctions == [1]
travaux.etat.update(manuel=False, reveil_auto=False)

# Suppression d'un compte par l'admin : livres, fichiers et livres audio partent avec lui
bob_id = next(u["id"] for u in json.loads(appel("/api/admin/utilisateurs", cookie=admin)[1]) if u["identifiant"] == "bob")
assert appel(f"/api/admin/utilisateurs/{bob_id}", cookie=alice, methode="DELETE")[0] == 403
assert appel(f"/api/admin/utilisateurs/{bob_id}", cookie=admin, methode="DELETE")[0] == 200
assert appel("/api/livres", cookie=bob)[0] == 401
assert not (base.AUDIO / str(lid_bob)).exists() and not (base.LIVRES / str(bob_id)).exists()
travaux.VOICESTUDIO_CLE = ""

# L'admin peut supprimer le livre de quelqu'un
code, corps, _ = appel("/api/livres", cookie=alice, brut=doc.tobytes(), entetes={"Content-Type": "application/pdf", "X-Nom": "Autre.pdf"})
lid2 = json.loads(corps)["id"]
assert appel(f"/api/livres/{lid2}", cookie=admin, methode="DELETE")[0] == 200
assert appel(f"/api/livres/{lid2}", cookie=alice)[0] == 404

# Révocation : la session d'Alice tombe immédiatement
appel("/api/admin/statut", {"id": alice_id, "statut": "refuse"}, cookie=admin)
assert appel("/api/livres", cookie=alice)[0] == 401
appel("/api/admin/statut", {"id": alice_id, "statut": "valide"}, cookie=admin)
_, alice = connexion("alice", "motdepasse1")

# Suppression
assert appel(f"/api/livres/{lid}", cookie=alice, methode="DELETE")[0] == 200
assert appel(f"/api/livres/{lid}", cookie=alice)[0] == 404 and not (base.LIVRES / str(alice_id) / f"{lid}.pdf").exists()
assert not (base.AUDIO / str(lid)).exists()
assert json.loads(appel("/api/stats", cookie=alice)[1])["secondes"] == 990   # les minutes survivent au livre supprimé

# Blocage après 5 mauvais mots de passe
for _ in range(5):
    assert connexion("bob", "mauvaismdp")[0] == 401
assert connexion("bob", "mauvaismdp")[0] == 429

# Déconnexion
appel("/api/deconnexion", {}, cookie=alice)
assert appel("/api/livres", cookie=alice)[0] == 401

# Durée de vie : un livre part 1 mois après sa dernière écoute, sa position est gardée 2 mois
_, alice = connexion("alice", "motdepasse1")
autre = pymupdf.open()
autre.new_page().insert_text((72, 72), "Premier. Second. Troisième.")
autre = autre.tobytes()


def importer(nom, pdf=autre):
    code, corps, _ = appel("/api/livres", cookie=alice, brut=pdf, entetes={"Content-Type": "application/pdf", "X-Nom": nom})
    assert code == 201, corps
    return json.loads(corps)


def vieillir(lid, jours, colonne="lu_le"):
    c = sqlite3.connect(base.BASE)
    c.execute(f"UPDATE livres SET {colonne} = ? WHERE id = ?", (int(time.time()) - jours * 86400, lid))
    c.commit()
    c.close()


vieux = importer("Vieux.pdf")["id"]
appel(f"/api/livres/{vieux}/progression", {"phrase": 2, "total": 3, "vitesse": 1.5}, cookie=alice)
base.ecrire_audio(vieux, 0, b"x", "wav")
appel(f"/api/livres/{vieux}/couverture", cookie=alice, brut=png, entetes={"Content-Type": "image/png"})
recent = importer("Récent.pdf", doc.tobytes())["id"]
jamais_lu = importer("Jamais lu.pdf", doc.tobytes())["id"]
en_cours = importer("En cours.pdf", doc.tobytes())["id"]
vieillir(vieux, 31)
vieillir(recent, 29)
vieillir(jamais_lu, 31, "ajoute_le")
vieillir(en_cours, 31)
base.definir_audio(en_cours, "encours")
assert base.purger() == 2
restants = {l["id"] for l in json.loads(appel("/api/livres", cookie=alice)[1])}
assert vieux not in restants and jamais_lu not in restants and {recent, en_cours} <= restants
assert not (base.AUDIO / str(vieux)).exists() and not list(base.COUVERTURES.iterdir())
assert not (base.LIVRES / str(alice_id) / f"{vieux}.pdf").exists()
r = importer("Vieux (encore).pdf")                       # même PDF réimporté : la position revient
assert r["repris"] is True
retrouve = json.loads(appel(f"/api/livres/{r['id']}", cookie=alice)[1])
assert (retrouve["phrase"], retrouve["vitesse"]) == (2, 1.5) and retrouve["lu_le"]
assert importer("Vieux (3).pdf")["repris"] is False      # une position ne sert qu'une fois
vieillir(r["id"], 31)
base.purger()
c = sqlite3.connect(base.BASE)
c.execute("UPDATE positions_gardees SET lu_le = ?", (int(time.time()) - 61 * 86400,))
c.commit()
c.close()
base.purger()                                             # plus de 2 mois après la dernière écoute : oubliée
assert importer("Vieux (4).pdf")["repris"] is False

# Présentation des nouveautés : à voir une fois, puis plus jamais
assert json.loads(appel("/api/moi", cookie=alice)[1])["voir_nouveautes"] == 1
assert appel("/api/moi/nouveautes", {}, cookie=alice)[0] == 200
assert json.loads(appel("/api/moi", cookie=alice)[1])["voir_nouveautes"] == 0
assert appel("/api/moi/nouveautes", {})[0] == 401

# Ruban du lecteur : carte des phrases déjà en stock en voix IA
lid_r = importer("Ruban.pdf", long.tobytes())["id"]
base.ecrire_audio(lid_r, 2, b"x", "wav")
carte = json.loads(appel(f"/api/livres/{lid_r}/carte", cookie=alice)[1])
assert carte == {"etat": None, "voix": None, "carte": "00100000"}, carte
assert appel(f"/api/livres/{lid_r}/carte", cookie=admin)[0] == 200
base.creer_utilisateur("carole", "motdepasse3", "valide")
_, carole = connexion("carole", "motdepasse3")
assert appel(f"/api/livres/{lid_r}/carte", cookie=carole)[0] == 404

# L'admin remet une progression à zéro, oublie les positions gardées, vide une bibliothèque
appel(f"/api/livres/{lid_r}/progression", {"phrase": 5, "total": 8, "vitesse": 1}, cookie=alice)
assert appel(f"/api/admin/livres/{lid_r}/progression", {}, cookie=alice)[0] == 403
assert appel(f"/api/admin/livres/{lid_r}/progression", {}, cookie=admin)[0] == 200
assert json.loads(appel(f"/api/livres/{lid_r}", cookie=alice)[1])["phrase"] == 0
assert appel("/api/admin/livres/99999/progression", {}, cookie=admin)[0] == 404
appel(f"/api/livres/{lid_r}/progression", {"phrase": 3, "total": 8, "vitesse": 1}, cookie=alice)
vieillir(lid_r, 31)
base.purger()                                             # le livre part, sa position est gardée
compte_alice = next(c for c in json.loads(appel("/api/admin/utilisateurs", cookie=admin)[1]) if c["identifiant"] == "alice")
assert compte_alice["positions"] == 1 and compte_alice["connecte_le"] and "octets" in compte_alice
assert appel(f"/api/admin/utilisateurs/{alice_id}/positions", cookie=alice, methode="DELETE")[0] == 403
assert appel(f"/api/admin/utilisateurs/{alice_id}/positions", cookie=admin, methode="DELETE")[0] == 200
assert importer("Ruban (réimport).pdf", long.tobytes())["repris"] is False
assert json.loads(appel(f"/api/livres", cookie=alice)[1])
assert appel(f"/api/admin/utilisateurs/{alice_id}/livres", cookie=alice, methode="DELETE")[0] == 403
assert appel(f"/api/admin/utilisateurs/{alice_id}/livres", cookie=admin, methode="DELETE")[0] == 200
assert json.loads(appel("/api/livres", cookie=alice)[1]) == [] and appel("/api/moi", cookie=alice)[0] == 200   # le compte reste
assert not list((base.LIVRES / str(alice_id)).glob("*.pdf"))

# Migration : un livre importé avant le découpage serveur est découpé au démarrage, sa progression recalée
c = sqlite3.connect(base.BASE)
c.execute("INSERT INTO livres (utilisateur, titre, paragraphes, phrase, total, ajoute_le) VALUES (?, 'Ancien', ?, 2, 4, 0)",
          (alice_id, json.dumps(["Un. Deux. Trois.", "Quatre. Cinq. Six."])))
c.commit()
c.close()
base.initialiser()
_, alice = connexion("alice", "motdepasse1")
ancien = next(l for l in json.loads(appel("/api/livres", cookie=alice)[1]) if l["titre"] == "Ancien")
assert (ancien["phrase"], ancien["total"]) == (3, 6), ancien  # à mi-livre avant, à mi-livre après

httpd.shutdown()
print("OK : tous les tests passent")
