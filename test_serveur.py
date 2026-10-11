"""Test de bout en bout de l'API, sur une base jetable. Lancer : python test_serveur.py"""
import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf

import base
import serveur

tmp = Path(tempfile.mkdtemp())
base.DONNEES, base.BASE, base.LIVRES = tmp, tmp / "test.db", tmp / "livres"
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
assert livre["titre"] == "Mon livre" and "Bonjour" in livre["paragraphes"][0]
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
assert appel(f"/api/stats?utilisateur={admin_id}", cookie=alice)[0] == 403
assert appel("/api/admin/stats", cookie=alice)[0] == 403
assert json.loads(appel("/api/stats?utilisateur=tous", cookie=admin)[1])["secondes"] == 990
assert json.loads(appel(f"/api/stats?utilisateur={alice_id}", cookie=admin)[1])["secondes"] == 990
lignes = {l["identifiant"]: l for l in json.loads(appel("/api/admin/stats", cookie=admin)[1])}
assert lignes["alice"]["secondes"] == 990 and lignes["alice"]["termines"] == 1 and lignes["admin"]["secondes"] == 0
assert appel("/api/stats?utilisateur=abc", cookie=admin)[0] == 400

# Isolation : l'admin ne voit pas les livres d'Alice
assert appel(f"/api/livres/{lid}", cookie=admin)[0] == 404
assert json.loads(appel("/api/livres", cookie=admin)[1]) == []
appel(f"/api/livres/{lid}", cookie=admin, methode="DELETE")
assert appel(f"/api/livres/{lid}", cookie=alice)[0] == 200

# Révocation : la session d'Alice tombe immédiatement
appel("/api/admin/statut", {"id": alice_id, "statut": "refuse"}, cookie=admin)
assert appel("/api/livres", cookie=alice)[0] == 401
appel("/api/admin/statut", {"id": alice_id, "statut": "valide"}, cookie=admin)
_, alice = connexion("alice", "motdepasse1")

# Suppression
assert appel(f"/api/livres/{lid}", cookie=alice, methode="DELETE")[0] == 200
assert appel(f"/api/livres/{lid}", cookie=alice)[0] == 404 and not (base.LIVRES / str(alice_id) / f"{lid}.pdf").exists()
assert json.loads(appel("/api/stats", cookie=alice)[1])["secondes"] == 990   # les minutes survivent au livre supprimé

# Blocage après 5 mauvais mots de passe
for _ in range(5):
    assert connexion("bob", "mauvaismdp")[0] == 401
assert connexion("bob", "mauvaismdp")[0] == 429

# Déconnexion
appel("/api/deconnexion", {}, cookie=alice)
assert appel("/api/livres", cookie=alice)[0] == 401

httpd.shutdown()
print("OK : tous les tests passent")
