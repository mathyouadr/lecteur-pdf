// Outils partagés par toutes les pages
const $ = id => document.getElementById(id);

// Appel à l'API : renvoie le JSON (ou un Blob pour l'audio), lève une Error avec le message du serveur
async function api(url, options = {}) {
  const r = await fetch(url, options);
  if (r.status === 401 && !location.pathname.endsWith("/connexion.html")) {
    location.href = "/connexion.html";
    throw new Error("Connexion requise.");
  }
  const type = r.headers.get("Content-Type") || "";
  const donnees = type.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) throw new Error(donnees.erreur || `Erreur ${r.status}`);
  return donnees;
}

const envoi = (donnees, extra = {}) => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(donnees), ...extra,
});

// Couverture générée : la couleur dépend du titre, toujours la même pour un livre donné
function teinte(titre) {
  let h = 0;
  for (const c of titre) h = (h * 31 + c.codePointAt(0)) % 360;
  return h;
}

// Livre l : la couverture importée par la personne si elle existe, sinon la couverture générée
function couverture(l) {
  const h = teinte(l.titre);
  const div = document.createElement("div");
  div.className = "couv";
  div.style.background = `linear-gradient(150deg, hsl(${h} 55% 45%), hsl(${(h + 35) % 360} 60% 14%))`;
  div.style.setProperty("--lueur", `hsl(${h} 80% 55% / .45)`);
  const t = document.createElement("div");
  t.className = "t";
  t.textContent = l.titre;
  const filet = document.createElement("div");
  filet.className = "filet";
  div.append(t, filet);
  if (l.couverture) {
    const img = new Image();
    img.className = "image";
    img.alt = "";
    img.src = `/api/livres/${l.id}/couverture?${l.couverture}`;
    img.onerror = () => { img.remove(); div.classList.remove("avec-image"); };
    div.classList.add("avec-image");
    div.prepend(img);
  }
  return div;
}

// État du livre audio IA, en une ligne, ou "" s'il n'y en a pas
function texteAudio(l) {
  const p = l.total ? Math.floor((l.audio_faits / l.total) * 100) : 0;
  return { attente: "Livre audio en attente", encours: `Livre audio : ${p} %`, pause: `Livre audio en pause (${p} %)`,
           pret: "Livre audio prêt", erreur: "Livre audio : erreur" }[l.audio_etat] || "";
}

const octetsTexte = o => !o ? "vide" : o < 1e6 ? `${Math.max(1, Math.round(o / 1e3))} Ko`
  : o > 1e9 ? `${(o / 1e9).toFixed(1).replace(".", ",")} Go` : `${Math.round(o / 1e6)} Mo`;

function barre(fraction) {
  const p = document.createElement("div");
  p.className = "prog";
  const i = document.createElement("i");
  i.style.width = `${Math.round(fraction * 100)}%`;
  if (fraction >= 1) i.className = "fini";
  p.append(i);
  return p;
}

const pourcent = l => (l.total ? Math.min(l.phrase / l.total, 1) : 0);
const vitesseTexte = v => `${String(+v).replace(".", ",")}×`;

async function deconnexion() {
  await fetch("/api/deconnexion", { method: "POST" });
  location.href = "/connexion.html";
}

function el(balise, classe, texte) {
  const e = document.createElement(balise);
  if (classe) e.className = classe;
  if (texte !== undefined) e.textContent = texte;
  return e;
}

const joursRestants = l => Math.max(0, Math.ceil(((l.lu_le || l.ajoute_le) + 30 * 86400 - Date.now() / 1000) / 86400));

// ---------- Notifications brèves ----------
function toast(texte, erreur = false) {
  let zone = $("toasts");
  if (!zone) {
    zone = el("div");
    zone.id = "toasts";
    zone.setAttribute("role", "status");
    zone.setAttribute("aria-live", "polite");
    document.body.append(zone);
  }
  const t = el("div", "toast" + (erreur ? " erreur" : ""), texte);
  zone.append(t);
  setTimeout(() => t.remove(), erreur ? 7000 : 3500);
}

// ---------- Menu d'actions (même fenêtre sur ordinateur et au doigt) ----------
// actions : [{ texte, aide?, danger?, question?, faire: async () => {} }]
function menuActions(titre, sousTitre, actions) {
  let d = $("menu-actions");
  if (!d) {
    d = el("dialog", "menu-actions");
    d.id = "menu-actions";
    d.addEventListener("click", e => { if (e.target === d) d.close(); });   // clic à côté : fermer
    document.body.append(d);
  }
  const liste = el("div", "actions");
  for (const a of actions) {
    const b = el("button", "action" + (a.danger ? " danger" : ""));
    b.append(el("span", "", a.texte));
    if (a.aide) b.append(el("small", "", a.aide));
    b.onclick = async () => {
      if (a.question && !confirm(a.question)) return;
      d.close();
      try { await a.faire(); } catch (err) { toast(err.message, true); }
    };
    liste.append(b);
  }
  const fermer = el("button", "fermer", "Fermer");
  fermer.onclick = () => d.close();
  d.replaceChildren(el("h2", "", titre), ...(sousTitre ? [el("p", "discret", sousTitre)] : []), liste, fermer);
  d.showModal();
}

// ---------- Présentation des nouveautés, une fois par compte ----------
const NOUVEAUTES = [
  { titre: "Tes livres lus par une voix IA",
    texte: "Chaque livre ajouté devient un livre audio, préparé sur notre serveur : tu peux l'écouter pendant qu'il se prépare. Pour changer de voix, ouvre le menu du livre et choisis « Livre audio » : écoute les démos et prends celle qui te plaît.",
    visuel: () => { const v = el("div", "visuel-onde"); for (let i = 0; i < 24; i++) v.append(el("i")); return v; } },
  { titre: "Ta propre couverture",
    texte: "Dans le menu d'un livre, « Changer la couverture » remplace la couverture générée par l'image de ton choix (JPEG, PNG ou WebP).",
    visuel: () => {
      const v = el("div", "visuel-couvertures");
      for (const titre of ["Germinal", "Notre-Dame de Paris", "Les Misérables"]) v.append(couverture({ titre }));
      return v;
    } },
  { titre: "Une bibliothèque qui reste légère",
    texte: "Un livre est supprimé 1 mois après ta dernière écoute. Ta position est gardée 2 mois : réimporte le même PDF et tu reprends à la bonne phrase.",
    visuel: () => {
      const v = el("div", "visuel-frise");
      v.append(el("span", "repere debut", "Dernière écoute"), el("span", "repere mois", "1 mois : le livre est supprimé"),
               el("span", "repere deux", "2 mois : la position est oubliée"));
      return v;
    } },
];

function presenterNouveautes() {
  const d = el("dialog", "nouveautes");
  d.setAttribute("aria-labelledby", "nouveautes-titre");
  let etape = 0;
  const afficher = () => {
    const n = NOUVEAUTES[etape];
    const points = el("div", "points");
    NOUVEAUTES.forEach((_, i) => points.append(el("i", i === etape ? "actif" : "")));
    const titre = el("h2", "", n.titre);
    titre.id = "nouveautes-titre";
    const suivant = el("button", "principal", etape < NOUVEAUTES.length - 1 ? "Suivant" : "C'est parti");
    suivant.onclick = () => { if (etape < NOUVEAUTES.length - 1) { etape++; afficher(); } else d.close(); };
    const passer = el("button", "lien", "Passer");
    passer.onclick = () => d.close();
    passer.hidden = etape === NOUVEAUTES.length - 1;
    const pied = el("div", "pied");
    pied.append(points, passer, suivant);
    d.replaceChildren(el("p", "sur", "Nouveau dans le lecteur"), n.visuel(), titre, el("p", "", n.texte), pied);
    suivant.focus();
  };
  d.addEventListener("close", () => { fetch("/api/moi/nouveautes", { method: "POST" }); d.remove(); });
  document.body.append(d);
  afficher();
  d.showModal();
}
