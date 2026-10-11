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

function couverture(titre) {
  const h = teinte(titre);
  const div = document.createElement("div");
  div.className = "couv";
  div.style.background = `linear-gradient(150deg, hsl(${h} 55% 45%), hsl(${(h + 35) % 360} 60% 14%))`;
  div.style.setProperty("--lueur", `hsl(${h} 80% 55% / .45)`);
  const t = document.createElement("div");
  t.className = "t";
  t.textContent = titre;
  const filet = document.createElement("div");
  filet.className = "filet";
  div.append(t, filet);
  return div;
}

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
