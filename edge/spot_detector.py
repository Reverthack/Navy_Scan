"""Détection de petites cibles par contraste local, sur image thermique (edge).

Méthode simple et testable : contraste = flou fin - flou large, seuil statistique
(médiane + k × MAD), puis composantes connexées filtrées par taille.

Ce n'est PAS l'implémentation de l'article HGH (Max-Tree + a contrario) : c'est une
base de travail, à comparer sur des enregistrements réels avant tout choix.
Ne nécessite aucune caméra : il suffit d'une image numpy (niveaux de gris).
"""
from collections import deque

import numpy as np


def box_blur(img: np.ndarray, radius: int) -> np.ndarray:
    """Flou moyenneur séparable, bords répliqués. Retourne un tableau float64."""
    if radius <= 0:
        return img.astype(np.float64)
    k = 2 * radius + 1
    padded = np.pad(img.astype(np.float64), radius, mode="edge")

    c = np.pad(np.cumsum(padded, axis=1), ((0, 0), (1, 0)))
    horiz = (c[:, k:] - c[:, :-k]) / k

    c = np.pad(np.cumsum(horiz, axis=0), ((1, 0), (0, 0)))
    return (c[k:, :] - c[:-k, :]) / k


def local_contrast(img: np.ndarray) -> np.ndarray:
    """Cibles claires plus petites que ~6 px sur fond local."""
    return box_blur(img, 1) - box_blur(img, 6)


def detect_spots(img: np.ndarray, k: float = 4.0, min_area: int = 3, max_area: int = 200) -> list:
    """Retourne une liste de dicts {x, y, area, contrast} (coordonnées pixel)."""
    if img.ndim != 2:
        raise ValueError("image en niveaux de gris attendue (2D)")

    contrast = local_contrast(img)
    median = np.median(contrast)
    mad = np.median(np.abs(contrast - median)) * 1.4826
    threshold = median + k * max(mad, 1e-9)
    mask = contrast > threshold

    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    spots = []

    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        queue = deque([(int(y0), int(x0))])
        seen[y0, x0] = True
        pixels = []
        while queue:
            y, x = queue.popleft()
            pixels.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))

        if min_area <= len(pixels) <= max_area:
            ys = [p[0] for p in pixels]
            xs = [p[1] for p in pixels]
            spots.append({
                "x": float(np.mean(xs)),
                "y": float(np.mean(ys)),
                "area": len(pixels),
                "contrast": float(max(contrast[y, x] for y, x in pixels)),
            })
    return spots
