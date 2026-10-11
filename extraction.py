"""Extrait le texte d'un PDF en paragraphes propres, prêts à être lus à voix haute."""
import re
import unicodedata

import pymupdf


def paragraphes(donnees: bytes, premiere: int = 1, derniere: int | None = None) -> list[str]:
    doc = pymupdf.open(stream=donnees, filetype="pdf")
    sortie, courant = [], ""
    for page in doc.pages(premiere - 1, derniere or doc.page_count):
        lignes = []
        for bloc in page.get_text("dict")["blocks"]:
            for ligne in bloc.get("lines", []):
                texte = "".join(s["text"] for s in ligne["spans"]).strip()
                if texte and not texte.isdigit() and not re.fullmatch(r"[ivxlcdm]+", texte):  # numéros de page
                    lignes.append((texte, ligne["bbox"][0], ligne["bbox"][2]))
        if not lignes:
            continue
        gauche = min(x0 for _, x0, _ in lignes)
        droite = max(x1 for _, _, x1 in lignes)
        largeur = droite - gauche
        for texte, x0, x1 in lignes:
            # Une ligne nettement en retrait commence un nouveau paragraphe
            if courant and x0 - gauche > 0.03 * largeur:
                sortie.append(courant)
                courant = ""
            if courant.endswith("-") and texte[:1].islower():
                courant = courant[:-1] + texte          # mot coupé en fin de ligne
            else:
                courant = f"{courant} {texte}".strip()
            # Une ligne qui s'arrête bien avant la marge droite termine le paragraphe
            if droite - x1 > 0.15 * largeur:
                sortie.append(courant)
                courant = ""
    if courant:
        sortie.append(courant)
    return [re.sub(r"\s+", " ", unicodedata.normalize("NFKC", p)) for p in sortie]  # NFKC : ligatures ﬁ ﬃ → fi ffi


if __name__ == "__main__":
    import sys
    for p in paragraphes(open(sys.argv[1], "rb").read())[:int(sys.argv[2]) if len(sys.argv) > 2 else 20]:
        print("¶", p)
