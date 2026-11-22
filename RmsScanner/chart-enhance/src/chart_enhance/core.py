"""Bounded, non-generative chart geometry and enhancement."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_OUTPUT = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ChartError(ValueError):
    pass


def load_image(path):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ChartError("Missing image or file exceeds 100 MiB")
    try:
        with Image.open(path) as im:
            if im.width * im.height > MAX_PIXELS:
                raise ChartError("Image exceeds 40 million pixels")
            return np.asarray(ImageOps.exif_transpose(im).convert("RGB")).copy()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ChartError(f"Cannot decode image: {exc}") from exc


def validate_quad(corners, shape):
    q = np.asarray(corners, dtype=np.float64)
    h, w = shape[:2]
    if q.shape != (4, 2) or not np.isfinite(q).all():
        raise ChartError("Four finite x,y corners required: TL, TR, BR, BL")
    if (q < 0).any() or (q[:, 0] > w - 1).any() or (q[:, 1] > h - 1).any():
        raise ChartError("Corners outside EXIF-oriented source image")
    a = np.roll(q, -1, axis=0) - q
    b = np.roll(q, -2, axis=0) - np.roll(q, -1, axis=0)
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    if not (cross > 0).all() or cv2.contourArea(q.astype("float32")) < 100:
        raise ChartError("Corners must be clockwise TL, TR, BR, BL; nondegenerate and convex")
    lengths = np.linalg.norm(np.roll(q, -1, axis=0) - q, axis=1)
    if lengths.min() < 10 or lengths.max() / lengths.min() > 30:
        raise ChartError("Degenerate or extreme quadrilateral")
    return q.astype(np.float32)


def ordered(points):
    p = np.asarray(points, np.float32).reshape(4, 2)
    p = p[np.argsort(np.arctan2(p[:, 1] - p[:, 1].mean(), p[:, 0] - p[:, 0].mean()))]
    return np.roll(p, -np.argmin(p.sum(axis=1)), axis=0)


def detect(rgb, _enhanced=False):
    h, w = rgb.shape[:2]
    scale = min(1, 1200 / max(h, w))
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    sh, sw = gray.shape
    edges = cv2.Canny(gray, 35, 110)
    # Low-contrast photographed axes need local contrast before line voting.
    if _enhanced:
        enhanced = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8)).apply(gray)
        edges |= cv2.Canny(enhanced, 30, 90)
    candidates = []
    for mask in (edges, cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)[1]):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            if cv2.contourArea(c) < sh * sw * 0.07:
                continue
            approx = cv2.approxPolyDP(c, 0.018 * cv2.arcLength(c, True), True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                candidates.append(ordered(approx))
    # Hough intersections supply frames interrupted by text/curves or weak borders.
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, 65, minLineLength=min(sh, sw) * 0.28, maxLineGap=35
    )
    hs, vs = [], []
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            dx, dy = x2 - x1, y2 - y1
            if abs(dy) < abs(dx) * 0.22:
                hs.append((math.hypot(dx, dy), (int(x1), int(y1), int(x2), int(y2))))
            elif abs(dx) < abs(dy) * 0.22:
                vs.append((math.hypot(dx, dy), (int(x1), int(y1), int(x2), int(y2))))

    def distinct(ls, horizontal):
        result = []
        for _, line in sorted(ls, reverse=True):
            mid = (line[1] + line[3]) / 2 if horizontal else (line[0] + line[2]) / 2
            if all(abs(mid - m) > min(sh, sw) * 0.035 for m, _ in result):
                result.append((mid, line))
        return sorted(result[:8])

    hs, vs = distinct(hs, True), distinct(vs, False)

    def intersect(a, b):
        a = np.cross([a[0], a[1], 1], [a[2], a[3], 1])
        b = np.cross([b[0], b[1], 1], [b[2], b[3], 1])
        p = np.cross(a, b)
        return p[:2] / p[2] if abs(p[2]) > 1e-6 else [-100, -100]

    for i, (_, top) in enumerate(hs):
        for _, bottom in hs[i + 1 :]:
            for j, (_, left) in enumerate(vs):
                for _, right in vs[j + 1 :]:
                    candidates.append(
                        np.float32(
                            [
                                intersect(top, left),
                                intersect(top, right),
                                intersect(bottom, right),
                                intersect(bottom, left),
                            ]
                        )
                    )
    contour_candidates = []
    for mask in (edges, cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)[1]):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            approx = cv2.approxPolyDP(contour, 0.018 * cv2.arcLength(contour, True), True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                contour_candidates.append(ordered(approx))
    ranked = []
    dilated = cv2.dilate(edges, np.ones((5, 5), np.uint8))
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    for q in candidates:
        try:
            validate_quad(q, small.shape)
        except ChartError:
            continue
        area = cv2.contourArea(q) / (sh * sw)
        if not 0.09 < area < 0.94:
            continue
        lengths = np.linalg.norm(np.roll(q, -1, axis=0) - q, axis=1)
        ratio = (lengths[0] + lengths[2]) / (lengths[1] + lengths[3])
        if not 0.55 < ratio < 4:
            continue
        support = []
        for a, b in zip(q, np.roll(q, -1, axis=0)):
            samples = np.linspace(a, b, 160).astype(int)
            support.append(float(np.mean(dilated[samples[:, 1], samples[:, 0]] > 0)))
        mask = np.zeros_like(gray)
        inner = q.mean(axis=0) + (q - q.mean(axis=0)) * 0.92
        cv2.fillConvexPoly(mask, inner.astype(int), 255)
        pixels = mask > 0
        color = float(np.mean((hsv[:, :, 1][pixels] > 50) & (hsv[:, :, 2][pixels] < 245)))
        local_background = cv2.dilate(gray, np.ones((15, 15), np.uint8))
        ink = float(np.mean((local_background.astype(float) - gray)[pixels] > 25))
        # Grid evidence is multiple internal long lines, not a menu's small boxed controls.
        internal = 0
        for _, line in hs + vs:
            mid = ((line[0] + line[2]) / 2, (line[1] + line[3]) / 2)
            if cv2.pointPolygonTest(inner.astype("float32"), mid, False) > 0:
                internal += 1
        score = (
            0.38 * np.mean(support)
            + 0.12 * min(support)
            + 0.16 * min(area / 0.45, 1)
            + 0.16 * min(color / 0.012, 1)
            + 0.12 * min(internal / 4, 1)
            + 0.06 * min(ink / 0.02, 1)
        )
        closed_frame = any(np.mean(np.linalg.norm(q - c, axis=1)) < 7 for c in contour_candidates)
        score += 0.14 if closed_frame else 0
        if color > 0.09:
            score -= 0.1  # Broad saturated UI regions are not fine-curve evidence.
        if ink > 0.38:
            score -= 0.2
        if min(support) < 0.35:
            score -= 0.15
        if any(np.mean(np.linalg.norm(q - r["corners_small"], axis=1)) < 15 for r in ranked):
            continue
        ranked.append(
            dict(
                corners_small=q,
                score=float(score),
                area=float(area),
                border_support=support,
                color_fraction=color,
                internal_lines=internal,
                closed_frame=closed_frame,
            )
        )
    # An enclosing colored screen/header is not the plot when a supported inner
    # frame excludes that broad UI color. Grid subcells lack the closed-frame prior.
    for outer in ranked:
        for inner in ranked:
            if not (0.45 < inner["area"] / outer["area"] < 0.88):
                continue
            if (
                inner["score"] < 0.85
                or outer["color_fraction"] < inner["color_fraction"] * 2 + 0.03
            ):
                continue
            oq, iq = outer["corners_small"], inner["corners_small"]
            if all(cv2.pointPolygonTest(oq, tuple(map(float, p)), False) >= 0 for p in iq):
                outer["score"] -= 0.16
                outer["exterior_frame_penalty"] = True
                break
    ranked.sort(key=lambda x: x["score"], reverse=True)
    evidence = []
    for r in ranked[:15]:
        evidence.append(
            {k: v for k, v in r.items() if k != "corners_small"}
            | {"corners": (r["corners_small"] / scale).tolist()}
        )
    if not ranked:
        return None, {
            "confidence": 0,
            "reason": "No supported chart quadrilateral",
            "candidates": [],
        }
    best, structural_conflict = select_candidate(ranked)
    # Establish complete grid endpoints independently of screen/menu size.
    coverage = {"repeated_rows": 0, "changed": False}
    supported = []
    for candidate in ranked[:30]:
        fitted, facts = grid_boundary(candidate["corners_small"], gray)
        if fitted is not None:
            supported.append((fitted, facts, candidate))
    if supported:
        fitted, coverage, best = max(supported, key=lambda item: cv2.contourArea(item[0]))
        width = np.linalg.norm(fitted[1] - fitted[0])
        frames = [
            f
            for f in contour_candidates
            if np.max(np.abs(f[:, 0] - fitted[:, 0])) < max(3, width * 0.006)
            and abs(f[2:, 1].mean() - fitted[2:, 1].mean()) < 4
            and 0 <= fitted[:2, 1].mean() - f[:2, 1].mean() < width * 0.13
        ]
        if frames:
            fitted = min(frames, key=lambda f: np.mean(np.abs(f[:, 0] - fitted[:, 0])))
        best = dict(best, corners_small=fitted)
    refined = None
    ok = best["score"] >= 0.71 and bool(supported)
    if not ok and not _enhanced:
        q, fallback = detect(rgb, _enhanced=True)
        fallback["weak_axis_fallback"] = True
        fallback["original_confidence"] = best["score"]
        return q, fallback
    return (best["corners_small"] / scale if ok else None), {
        "confidence": best["score"],
        "reason": (
            "accepted: strongest plot evidence"
            if ok and structural_conflict
            else "accepted"
            if ok
            else "Needs manual corners: weak chart evidence"
        ),
        "grid_coverage": coverage,
        "selection_policy": (
            "repeated_grid_endpoints"
            if supported
            else "supported_interior_rails"
            if refined is not None
            else "structural_hierarchy"
            if structural_conflict
            else "highest_score"
        ),
        "selected_corners": (best["corners_small"] / scale).tolist() if ok else None,
        "candidates": evidence,
    }


def grid_boundary(q, gray):
    choices = [_grid_boundary(q, gray, gap) for gap in (30, 70)]
    accepted = [(quad, facts) for quad, facts in choices if quad is not None]
    return max(accepted, key=lambda item: cv2.contourArea(item[0])) if accepted else choices[0]


def _grid_boundary(q, gray, gap):
    """Fit repeated grid endpoints, which distinguish plot borders from inner rails/UI."""
    width = float(np.linalg.norm(q[1] - q[0]))
    height = float(np.linalg.norm(q[3] - q[0]))
    offset = 300
    target = np.float32(
        [
            [offset, offset],
            [offset + width, offset],
            [offset + width, offset + height],
            [offset, offset + height],
        ]
    )
    transform = cv2.getPerspectiveTransform(q.astype(np.float32), target)
    size = (int(width + 2 * offset), int(height + 2 * offset))
    rect = cv2.warpPerspective(gray, transform, size, borderValue=255)
    ink = cv2.adaptiveThreshold(
        rect, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 2
    )
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, 85), np.uint8))
    horizontal = cv2.morphologyEx(horizontal, cv2.MORPH_CLOSE, np.ones((1, gap), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(horizontal)
    rows = []
    for x, y, w, h, _ in stats[1:]:
        if (
            w > width * 0.60
            and h < 12
            and offset - height * 0.30 < y < offset + height * 1.30
            and offset - width * 0.30 < x < offset + width * 0.25
            and offset + width * 0.65 < x + w < offset + width * 1.30
        ):
            rows.append((float(x), float(y + h / 2), float(x + w - 1)))
    evidence = {"repeated_rows": 0, "changed": False}
    if len(rows) < 5:
        return None, evidence
    clusters = []
    tolerance = max(5, width * 0.009)
    for row in rows:
        cluster = [
            r for r in rows if abs(r[0] - row[0]) < tolerance and abs(r[2] - row[2]) < tolerance
        ]
        if len(cluster) < 5:
            continue
        cluster = np.asarray(sorted(cluster, key=lambda r: r[1]))
        gaps = np.diff(cluster[:, 1])
        gaps = gaps[gaps > 8]
        if len(gaps) < 4:
            continue
        period = float(np.percentile(gaps, 30))
        phase = cluster[:, 1]
        groups = [
            cluster[np.abs((phase - y) / period - np.rint((phase - y) / period)) < 0.13]
            for y in phase
        ]
        regular = max(groups, key=len)
        if len(regular) >= 5 and np.ptp(regular[:, 1]) > height * 0.35:
            clusters.append((regular, period))
    if not clusters:
        return None, evidence
    cluster, period = max(
        clusters, key=lambda c: np.ptp(c[0][:, 1]) * np.median(c[0][:, 2] - c[0][:, 0])
    )
    left = np.polyfit(cluster[:, 1], cluster[:, 0], 1)
    right = np.polyfit(cluster[:, 1], cluster[:, 2], 1)
    top, bottom = cluster[0, 1], cluster[-1, 1]
    # Endpoint-aligned terminal horizontal rules delimit blank plot regions too.
    # Unlike the grid lattice they need not share its period.
    terminals = []
    for x, y, end in rows:
        if abs(x - np.polyval(left, y)) < 25 and abs(end - np.polyval(right, y)) < 25:
            terminals.append(y)
    top_terminals = [
        y
        for x, y, end in rows
        if abs(x - np.polyval(left, y)) < 12
        and abs(end - np.polyval(right, y)) < 12
        and top - period * 1.2 <= y <= top
    ]
    if top_terminals:
        top = min(top_terminals)
    # Missing/faint upper grid rows can leave the first periodic row well below
    # the frame. Recover a terminal only where BOTH fitted grid rails reach it;
    # a screen/header rule sharing approximate endpoints is not sufficient.
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((25, 1), np.uint8))
    rail_terminals = []
    for x, y, end in rows:
        if y >= top or abs(x - np.polyval(left, y)) >= 25:
            continue
        if abs(end - np.polyval(right, y)) >= 25:
            continue
        ys = np.arange(int(y), int(top) + 1)
        support = []
        for rail in (left, right):
            xs = np.rint(np.polyval(rail, ys)).astype(int)
            samples = np.stack(
                [vertical[ys, np.clip(xs + dx, 0, rect.shape[1] - 1)] for dx in range(-3, 4)]
            ).max(axis=0) > 0
            support.append(bool(samples[:3].all()) and np.mean(samples) >= 0.90)
        if all(support):
            rail_terminals.append(y)
    if rail_terminals:
        terminal = min(rail_terminals)
        # Use the observed rule's upper edge, not an arbitrary safety margin.
        top = min(
            float(y)
            for x, y, w, h, _ in stats[1:]
            if abs(y + h / 2 - terminal) < 0.01 and w > width * 0.60
        )
    if terminals:
        bottom = max(bottom, max(terminals))
    rect_quad = np.float32(
        [
            [np.polyval(left, top), top],
            [np.polyval(right, top), top],
            [np.polyval(right, bottom), bottom],
            [np.polyval(left, bottom), bottom],
        ]
    )
    fitted = cv2.perspectiveTransform(rect_quad[None], np.linalg.inv(transform))[0]
    evidence = {
        "repeated_rows": len(cluster),
        "period_small": period,
        "rectified_rows": cluster.tolist(),
        "rail_supported_top_terminals": rail_terminals,
        "rectified_top": float(top),
        "changed": False,
    }
    try:
        validate_quad(fitted, gray.shape)
    except ChartError:
        return None, evidence
    evidence["changed"] = True
    return fitted, evidence


def refine_boundary(outer, frames, horizontal_segments, verticals, edges):
    """Extend a large interior frame along its rails, not the enclosing UI rails."""
    outer_area = cv2.contourArea(outer)
    ow = np.linalg.norm(outer[1] - outer[0])
    oh = np.linalg.norm(outer[3] - outer[0])
    anchors = []
    for frame in frames:
        if not 0.45 < cv2.contourArea(frame) / outer_area < 0.88:
            continue
        if not all(cv2.pointPolygonTest(outer, tuple(map(float, p)), False) >= -3 for p in frame):
            continue
        fw = min(np.linalg.norm(frame[1] - frame[0]), np.linalg.norm(frame[2] - frame[3]))
        fh = min(np.linalg.norm(frame[3] - frame[0]), np.linalg.norm(frame[2] - frame[1]))
        if fw < ow * 0.65 or fh < oh * 0.55:
            continue
        # A nested frame must also share an independently voted vertical rail.
        if not any(abs(mid - frame[1:3, 0].mean()) < ow * 0.025 for mid, _ in verticals):
            continue
        anchors.append(frame)
    if not anchors:
        return None

    def frame_support(frame):
        support = []
        for a, b in ((frame[0], frame[3]), (frame[1], frame[2])):
            pts = np.rint(np.linspace(a, b, 200)).astype(int)
            support.append(float(np.mean(edges[pts[:, 1], pts[:, 0]] > 0)))
        return min(support)

    frame = max(anchors, key=frame_support)

    def rail_x(a, b, y):
        return a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1])

    spans = []
    for length, line in horizontal_segments:
        x1, y1, x2, y2 = line
        if x1 > x2:
            x1, y1, x2, y2 = x2, y2, x1, y1
        lx = rail_x(frame[0], frame[3], y1)
        rx = rail_x(frame[1], frame[2], y2)
        if abs(x1 - lx) > ow * 0.065 or abs(x2 - rx) > ow * 0.065:
            continue
        if length < (rx - lx) * 0.85:
            continue
        y = (y1 + y2) / 2
        if outer[:, 1].min() - 3 <= y <= frame[2:, 1].max() + oh * 0.06:
            spans.append((y, np.float32([[lx, y1], [rx, y2]])))
    if len(spans) < 2:
        return None
    # An enclosing header can share both vertical rails. Only locally extend
    # the closed plot frame's top, never jump to that unrelated header rail.
    local_top = [s for s in spans if abs(s[0] - frame[:2, 1].mean()) <= oh * 0.06]
    top = min(local_top, key=lambda s: s[0])[1] if local_top else frame[:2]
    bottom = max(spans, key=lambda s: s[0])[1]
    q = np.float32([top[0], top[1], bottom[1], bottom[0]])
    if cv2.contourArea(q) < outer_area * 0.4:
        return None
    # Require uninterrupted supporting rails through the extended region.
    for a, b in ((q[0], q[3]), (q[1], q[2])):
        pts = np.rint(np.linspace(a, b, 200)).astype(int)
        if (pts < 0).any() or (pts[:, 0] >= edges.shape[1]).any():
            return None
        if (pts[:, 1] >= edges.shape[0]).any():
            return None
        if np.mean(edges[pts[:, 1], pts[:, 0]] > 0) < 0.65:
            return None
    return q


def select_candidate(ranked):
    """Score and axis evidence outrank exterior size; retain tuple compatibility."""
    best = max(ranked, key=lambda r: r["score"])
    competitors = [r for r in ranked if r["score"] >= 0.71 and r["score"] > best["score"] - 0.025]
    conflict = any(
        np.mean(np.linalg.norm(r["corners_small"] - best["corners_small"], axis=1)) > 40
        for r in competitors
    )
    return best, conflict


def geometry(q):
    """Map exact source-frame endpoints without padding or downsampling."""
    width = max(np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3]))
    height = max(np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1]))
    ow, oh = max(10, math.ceil(width) + 1), max(10, math.ceil(height) + 1)
    if ow * oh > MAX_OUTPUT:
        raise ChartError("Output exceeds 40 million pixels")
    target = np.float32([[0, 0], [ow - 1, 0], [ow - 1, oh - 1], [0, oh - 1]])
    H = cv2.getPerspectiveTransform(q.astype(np.float32), target)
    inverse = np.linalg.inv(H)
    if not np.isfinite(H).all() or np.linalg.cond(H) > 1e9:
        raise ChartError("Unstable perspective mapping")
    return H, inverse, q.copy(), (ow, oh)


def background_model(rgb):
    x = rgb.astype(np.float32)
    h, w = rgb.shape[:2]
    small = cv2.resize(x, (max(8, w // 12), max(8, h // 12)), interpolation=cv2.INTER_AREA)
    field = cv2.GaussianBlur(cv2.dilate(small, np.ones((7, 7), np.uint8)), (0, 0), 3)
    field = cv2.resize(field, (w, h), interpolation=cv2.INTER_LINEAR)
    envelope = cv2.dilate(x, np.ones((9, 9), np.uint8))
    residual = np.max(envelope - x, axis=2)
    chroma = np.ptp(x - field, axis=2)
    blank = (residual < 3) & (chroma < 8) & (x.min(axis=2) > 100)
    return field, residual, chroma, blank


def whiten(rgb, strength=1):
    strength = float(strength)
    if not math.isfinite(strength):
        raise ChartError("Whitening strength must be finite")
    field, residual, chroma, _ = background_model(rgb)
    x = rgb.astype(np.float32)
    local_envelope = cv2.dilate(x, np.ones((9, 9), np.uint8))
    shift = np.maximum(255 - np.minimum(field, local_envelope), 0)
    weight = np.clip((x.min(axis=2) - 95) / 40, 0, 1)
    weight *= np.where(chroma < 12, 1.0, np.clip(1 - (chroma - 12) / 33, 0, 1))
    result = x + shift * weight[:, :, None] * np.clip(strength, 0, 1)
    # Preserve observed local stroke depth even if channel illumination clips.
    cap = 255 - residual
    neutral_structure = (chroma < 12) & (residual >= 3)
    result[neutral_structure] = np.minimum(result[neutral_structure], cap[neutral_structure, None])
    if strength == 0:
        return rgb.copy()
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def quality_metrics(before, after):
    _, residual, chroma, blank = background_model(before)
    g0 = cv2.cvtColor(before, cv2.COLOR_RGB2GRAY).astype(float)
    g1 = cv2.cvtColor(after, cv2.COLOR_RGB2GRAY).astype(float)
    grid = (residual >= 5) & (residual < 65) & (chroma < 12) & (g0 > 120)

    def contrast(g):
        return cv2.dilate(g.astype(np.float32), np.ones((9, 9), np.uint8)) - g

    c0, c1 = contrast(g0), contrast(g1)
    return {
        "blank_pixels": int(blank.sum()),
        "blank_fraction": float(blank.mean()),
        "background_median": float(np.median(g1[blank])) if blank.any() else None,
        "background_p10": float(np.percentile(g1[blank], 10)) if blank.any() else None,
        "grid_pixels": int(grid.sum()),
        "grid_contrast_before": float(np.median(c0[grid])) if grid.any() else None,
        "grid_contrast_after": float(np.median(c1[grid])) if grid.any() else None,
        "dark_pixels_before": int((g0 < 90).sum()),
        "dark_pixels_after": int((g1 < 90).sum()),
    }


def sharpen(rgb, strength=0.3):
    strength = float(strength)
    if not math.isfinite(strength):
        raise ChartError("Sharpen strength must be finite")
    strength = float(np.clip(strength, 0, 1))
    if strength == 0:
        return rgb.copy()
    x = rgb.astype(np.float32)
    blur = cv2.GaussianBlur(x, (0, 0), 0.8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edge = np.clip(cv2.Laplacian(gray, cv2.CV_32F).__abs__() / 30, 0, 1)
    delta = np.clip((x - blur) * strength * edge[:, :, None], -6, 6)
    lo = cv2.erode(rgb, np.ones((3, 3), np.uint8)).astype(float)
    hi = cv2.dilate(rgb, np.ones((3, 3), np.uint8)).astype(float)
    return np.clip(x + delta, lo, hi).astype(np.uint8)


def overlay(rgb, evidence, q=None, crop=None):
    image = rgb.copy()
    for r in evidence.get("candidates", [])[:5]:
        cv2.polylines(image, [np.int32(r["corners"])], True, (240, 160, 0), 3)
    if q is not None:
        cv2.polylines(image, [np.int32(q)], True, (0, 180, 30), 5)
    if crop is not None:
        cv2.polylines(image, [np.int32(crop)], True, (220, 20, 180), 4)
    return image


def process(path, output, corners=None, whitening=True, sharpen_strength=0.3):
    path = Path(path).resolve()
    output = Path(output).resolve()
    rgb = load_image(path)
    q, evidence = (
        detect(rgb) if corners is None else (corners, {"reason": "manual", "confidence": None})
    )
    output.mkdir(parents=True, exist_ok=True)
    job = output / (path.stem + "-" + uuid.uuid4().hex[:12])
    job.mkdir()
    if q is None:
        Image.fromarray(overlay(rgb, evidence)).save(job / "detection.png")
        (job / "diagnostic.json").write_text(json.dumps(evidence, indent=2))
        raise ChartError(f"{evidence['reason']}; diagnostic: {job / 'diagnostic.json'}")
    q = validate_quad(q, rgb.shape)
    H, inverse, crop, size = geometry(q)
    rectified = cv2.warpPerspective(
        rgb,
        H,
        size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    sharp = sharpen(rectified, sharpen_strength)
    white = whiten(sharp) if whitening else sharp.copy()
    final = white.copy()
    for name, image in [
        ("detection", overlay(rgb, evidence, q, crop)),
        ("rectified", rectified),
        ("sharpened", sharp),
        ("whitened", white),
        ("final", final),
    ]:
        Image.fromarray(image).save(job / f"{name}.png")
    h, w = rgb.shape[:2]
    record = {
        "protocol_version": 1,
        "stage": "chart-enhance",
        "status": "ok",
        "input": str(path),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "output": str(job / "final.png"),
        "original_size": [w, h],
        "processed_size": list(size),
        "output_size": list(size),
        "output_bytes": (job / "final.png").stat().st_size,
        "metadata": str(job / "meta.json"),
        "corners": q.tolist(),
        "crop_corners": crop.tolist(),
        "source_to_output": H.tolist(),
        "output_to_source": inverse.tolist(),
        "detection": evidence,
        "metrics": quality_metrics(rectified, final),
        "settings": {
            "margin_mode": "fixed-left-plot",
            "safety_padding": 0,
            "coordinate_padding": [0, 0, 0, 0],
            "stage_order": ["rectified", "sharpened", "whitened", "final"],
            "margins": [0, 0, 0, 0],
            "whiten": whitening,
            "sharpen_strength": float(np.clip(sharpen_strength, 0, 1)),
        },
        "warnings": [
            "No OCR or reconstruction; invisible/clipped source labels cannot be restored"
        ],
        "artifacts": {
            n: str(job / f"{n}.png")
            for n in ("detection", "rectified", "sharpened", "whitened", "final")
        },
    }
    (job / "meta.json").write_text(json.dumps(record, indent=2))
    return record
