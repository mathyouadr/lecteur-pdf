"""Génération des livres audio en tâche de fond, avec VoiceStudio sur srv-02.

Une seule file, traitée livre par livre, avec la voix que la personne a choisie dans le catalogue :
on réveille srv-02 (Wake-on-LAN) si VoiceStudio ne répond pas, on génère une phrase à la fois
(en commençant là où la personne en est), et chaque fichier est rangé
sur ce serveur : le livre audio reste écoutable quand srv-02 est éteint. Si c'est la file qui a réveillé
srv-02, on l'éteint 10 minutes après la fin du travail (jamais s'il a été allumé à la main).

Configuration (variables d'environnement, voir README) :
    VOICESTUDIO_URL, VOICESTUDIO_CLE      API VoiceStudio et sa clé admin (sans clé : pas de livres audio)
    SRV02_MAC                             adresse MAC de srv-02 pour le Wake-on-LAN
    PVE_URL, PVE_NOEUD, PVE_JETON, PVE_CA API Proxmox de srv-02 pour l'extinction (jeton limité à Sys.PowerMgmt)
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import base

VOICESTUDIO_URL = os.environ.get("VOICESTUDIO_URL", "http://192.168.1.45:3900").rstrip("/")
VOICESTUDIO_CLE = os.environ.get("VOICESTUDIO_CLE", "")
SRV02_MAC = os.environ.get("SRV02_MAC", "")
PVE_URL = os.environ.get("PVE_URL", "https://192.168.1.50:8006").rstrip("/")
PVE_NOEUD = os.environ.get("PVE_NOEUD", "srv-02")
PVE_JETON = os.environ.get("PVE_JETON", "")       # « utilisateur@pve!nom=secret »
PVE_CA = os.environ.get("PVE_CA", "")             # certificat de l'autorité du cluster (pve-root-ca.pem)

TEXTE_DEMO = ("C'était une belle et froide journée d'avril, et les horloges sonnaient treize heures. "
              "Voici la voix qui lira ton livre.")
ATTENTE_DEMARRAGE = 15 * 60   # au-delà, on signale que srv-02 ne répond pas (mais on continue d'essayer)
DELAI_EXTINCTION = 10 * 60
ESSAIS_PAR_PHRASE = 3

# reveil_auto : c'est la file qui a réveillé srv-02 (donc elle l'éteindra) ; manuel : allumé depuis le panel ;
# eteint_manuel : éteint depuis le panel, la file ne le rallume pas avant le prochain import ou un réveil manuel
# ecoute_vue : dernière génération à la demande pour quelqu'un qui écoute ; la file lui laisse la GPU
etat = {"message": "", "reveil_auto": False, "manuel": False, "eteint_manuel": False,
        "inactif_depuis": None, "reveil_envoye": 0.0, "vs_ok": False, "vs_vu": 0.0, "ecoute_vue": 0.0}
PRIORITE_ECOUTE = 8   # secondes sans demande de l'auditeur avant que la file reprenne la GPU
verrou = threading.Lock()
en_generation = set()          # (livre, phrase) en cours de génération, par la file ou à la demande du lecteur
verrou_phrases = threading.Lock()


def actif():
    return bool(VOICESTUDIO_CLE)


def extinction_possible():
    return bool(PVE_JETON)


# ---------- srv-02 ----------
def voicestudio_pret(recent=0):
    """recent=N : réutilise la dernière réponse si elle a moins de N secondes (évite d'attendre 3 s par phrase
    quand srv-02 dort et que le lecteur demande l'audio)."""
    if recent and time.time() - etat["vs_vu"] < recent:
        return etat["vs_ok"]
    try:
        with urllib.request.urlopen(f"{VOICESTUDIO_URL}/health", timeout=3) as r:
            ok = json.loads(r.read()).get("status") == "ok"
    except (OSError, ValueError):
        ok = False
    etat.update(vs_ok=ok, vs_vu=time.time())
    return ok


def reveiller():
    """Paquet magique Wake-on-LAN : 6 × FF puis 16 fois l'adresse MAC, en diffusion sur le port 9."""
    mac = bytes.fromhex(SRV02_MAC.replace(":", "").replace("-", ""))
    if len(mac) != 6:
        raise ValueError("SRV02_MAC invalide")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(b"\xff" * 6 + mac * 16, ("255.255.255.255", 9))
    etat["reveil_envoye"] = time.time()


def eteindre():
    """Arrêt propre de srv-02 par l'API Proxmox (le jeton n'a que le droit d'allumer/éteindre ce nœud)."""
    if not extinction_possible():
        raise RuntimeError("Extinction non configurée (PVE_JETON).")
    contexte = ssl.create_default_context(cafile=PVE_CA or None)
    contexte.check_hostname = False  # certificat du nœud émis pour son nom, on l'appelle par IP ; la CA reste vérifiée
    req = urllib.request.Request(f"{PVE_URL}/api2/json/nodes/{PVE_NOEUD}/status", method="POST",
                                 data=b"command=shutdown",
                                 headers={"Authorization": f"PVEAPIToken={PVE_JETON}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=10, context=contexte).close()


def reveiller_manuel():
    with verrou:
        etat.update(manuel=True, reveil_auto=False, eteint_manuel=False)
    reveiller()


def eteindre_manuel():
    eteindre()
    with verrou:
        etat.update(manuel=False, reveil_auto=False, eteint_manuel=True, inactif_depuis=None)


def nouvelle_demande():
    """Quelqu'un vient de demander un livre audio : la file a de nouveau le droit de réveiller srv-02."""
    etat["eteint_manuel"] = False


def etat_public():
    return {"configure": actif(), "wol": bool(SRV02_MAC), "extinction": extinction_possible(),
            "voicestudio": voicestudio_pret(), "manuel": etat["manuel"], "reveil_auto": etat["reveil_auto"],
            "message": etat["message"], "file": base.file_audio()}


# ---------- Voix ----------
def _multipart(champs):
    borne = uuid.uuid4().hex
    corps = b"".join(f'--{borne}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                     for k, v in champs.items())
    return corps + f"--{borne}--\r\n".encode(), f"multipart/form-data; boundary={borne}"


def synthese_ia(texte, v, delai=300):
    """Une phrase avec la voix v (instruct, seed). Renvoie (octets, extension) : MP3 si ffmpeg est installé,
    sinon le WAV brut."""
    corps, type_ = _multipart({"text": texte, "language": "French", "instruct": v["instruct"], "seed": v["seed"]})
    req = urllib.request.Request(f"{VOICESTUDIO_URL}/generate", data=corps, method="POST",
                                 headers={"Content-Type": type_, "Authorization": f"Bearer {VOICESTUDIO_CLE}"})
    with urllib.request.urlopen(req, timeout=delai) as r:
        wav = r.read()
    if not shutil.which("ffmpeg"):
        return wav, "wav"  # ponytail: ~10× plus lourd que le MP3, installer ffmpeg en production
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-b:a", "48k", "-f", "mp3", "pipe:1"],
                         input=wav, capture_output=True, check=True).stdout
    return mp3, "mp3"


def generer_phrase(lid, n, texte, v, delai=300):
    """Génère et range l'audio d'une phrase. Une même phrase n'est jamais générée deux fois en même temps :
    si la file s'en occupe déjà, on attend son fichier. Renvoie le fichier, ou None (lève l'erreur de VoiceStudio)."""
    with verrou_phrases:
        deja = (lid, n) in en_generation
        en_generation.add((lid, n))
    if deja:
        fin = time.time() + delai
        while (lid, n) in en_generation and time.time() < fin:
            time.sleep(0.2)
        return base.fichier_audio(None, lid, n)
    try:
        base.ecrire_audio(lid, n, *synthese_ia(texte[:2000], v, delai))
    finally:
        with verrou_phrases:
            en_generation.discard((lid, n))
    return base.fichier_audio(None, lid, n)


def phrase_a_la_demande(lid, n):
    """Le lecteur veut la phrase n et elle n'est pas encore prête : on la génère tout de suite (1 à 2 s sur la
    GPU) et elle reste en stock. None si ce n'est pas possible maintenant (srv-02 endormi, pas de voix choisie) :
    le lecteur passe alors sur la voix Google pour cette phrase."""
    info = base.info_audio(lid)
    if not (actif() and info and info["voix"] and info["audio_etat"] in ("attente", "encours", "erreur")):
        return None
    v = base.voix(info["voix"])
    phrases = base.phrases_a_plat(lid)
    if not v or not 0 <= n < len(phrases) or not voicestudio_pret(recent=10):
        return None
    etat["ecoute_vue"] = time.time()            # la file se met en retrait le temps de servir l'auditeur
    try:
        return generer_phrase(lid, n, phrases[n], v, delai=30)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    finally:
        etat["ecoute_vue"] = time.time()


def _ceder_a_l_ecoute(lid):
    """Pendant qu'un auditeur a besoin de la GPU (génération à la demande récente), la file attend."""
    while time.time() - etat["ecoute_vue"] < PRIORITE_ECOUTE and base.etat_audio(lid) == "encours":
        etat["message"] = "Quelqu'un écoute : la file laisse la priorité à la lecture."
        time.sleep(0.5)
    if etat["message"].startswith("Quelqu'un écoute"):
        etat["message"] = ""


# ---------- File de génération ----------
def _attendre_srv02():
    """True si VoiceStudio répond ; sinon réveille srv-02 (au plus une fois toutes les 5 min) et renvoie False."""
    if voicestudio_pret():
        return True
    if etat["eteint_manuel"]:
        etat["message"] = "srv-02 éteint depuis le panel : réveil au prochain import, ou avec le bouton « Réveiller »."
        return False
    if SRV02_MAC and time.time() - etat["reveil_envoye"] > 5 * 60:
        with verrou:
            if not etat["manuel"]:
                etat["reveil_auto"] = True
        try:
            reveiller()
        except (OSError, ValueError) as e:
            etat["message"] = f"Wake-on-LAN impossible : {e}"
            return False
    attente = time.time() - etat["reveil_envoye"]
    etat["message"] = ("srv-02 ne répond pas : vérifier qu'il est branché et que le Wake-on-LAN est actif."
                       if attente > ATTENTE_DEMARRAGE else "Réveil de srv-02…")
    return False


def _suivante(faites, position):
    """Prochaine phrase à générer : la première manquante à partir de là où la personne écoute, sinon depuis le début."""
    total = len(faites)
    position = min(max(position, 0), total)
    return next((n for n in [*range(position, total), *range(position)] if not faites[n]), None)


def _generer(lid, v):
    """Génère tout le livre en suivant l'auditeur : avant chaque phrase, on relit sa position, pour que ce
    qu'il va entendre soit prêt avant lui. Continue jusqu'au bout même quand personne n'écoute.
    Renvoie l'état final, ou None pour réessayer plus tard."""
    phrases = base.phrases_a_plat(lid)
    faites = [False] * len(phrases)
    for n in base.phrases_generees(lid):
        if n < len(faites):
            faites[n] = True
    compte = 0
    while True:
        _ceder_a_l_ecoute(lid)
        if base.etat_audio(lid) != "encours":       # mis en pause, annulé ou livre supprimé pendant ce temps
            return base.etat_audio(lid)
        n = _suivante(faites, base.position(lid))
        if n is None:
            manquantes = set(range(len(phrases))) - base.phrases_generees(lid)
            if not manquantes:
                return "pret"
            for m in manquantes:                      # une génération à la demande a échoué entre-temps
                faites[m] = False
            continue
        faites[n] = True
        for essai in range(ESSAIS_PAR_PHRASE):
            try:
                generer_phrase(lid, n, phrases[n], v)
                break
            except (OSError, subprocess.CalledProcessError, ValueError) as e:
                if not voicestudio_pret():
                    return None                      # srv-02 est tombé : on le réveillera au tour suivant
                if essai == ESSAIS_PAR_PHRASE - 1:
                    return f"erreur:{type(e).__name__} sur la phrase {n + 1} : {e}"[:300]
                time.sleep(5)
        compte += 1
        if compte % 10 == 0:
            base.compter_audio(lid)


def _demos():
    """Démos manquantes du catalogue, générées dès que VoiceStudio répond (sans réveiller srv-02 pour ça)."""
    manquantes = base.voix_sans_demo()
    if manquantes and voicestudio_pret():
        for v in manquantes:
            try:
                base.definir_demo(v["id"], *synthese_ia(TEXTE_DEMO, v))
            except (OSError, subprocess.CalledProcessError) as e:
                etat["message"] = f"Démo de la voix « {v['nom']} » impossible : {e}"


def _tour():
    _demos()
    travail = base.prochain_travail()
    if not travail:
        with verrou:
            etat["message"] = ""
            if etat["reveil_auto"] and not etat["manuel"] and extinction_possible():
                etat["inactif_depuis"] = etat["inactif_depuis"] or time.time()
                if time.time() - etat["inactif_depuis"] > DELAI_EXTINCTION:
                    try:
                        eteindre()
                        etat.update(reveil_auto=False, inactif_depuis=None)
                    except OSError as e:
                        etat["message"] = f"Extinction de srv-02 impossible : {e}"
        return False
    etat["inactif_depuis"] = None
    if not _attendre_srv02():
        return False
    etat["message"] = ""
    v = base.voix(travail["voix"])
    if not v:
        base.definir_audio(travail["id"], "erreur", "Cette voix n'existe plus : choisis-en une autre.")
        return True
    base.definir_audio(travail["id"], "encours")
    fin = _generer(travail["id"], v)
    if fin is None:
        return False
    if fin == "pret":
        base.definir_audio(travail["id"], "pret")
    elif fin.startswith("erreur:"):
        base.definir_audio(travail["id"], "erreur", fin[len("erreur:"):])
    else:
        base.compter_audio(travail["id"])           # pause ou annulation : l'état a déjà été posé par l'admin
    return True


def boucle():
    while True:
        try:
            travail_fait = _tour()
        except Exception as e:  # la file ne doit jamais mourir : on note l'erreur et on réessaie
            etat["message"] = f"Erreur de la file : {e}"
            travail_fait = False
        if not travail_fait:
            time.sleep(15)


def demarrer():
    if actif():
        threading.Thread(target=boucle, daemon=True, name="travaux").start()
