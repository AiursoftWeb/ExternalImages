import hashlib
import json

import cv2
import numpy as np
import pytest
from PIL import Image

from chart_enhance.core import (
    ChartError,
    detect,
    geometry,
    load_image,
    process,
    sharpen,
    validate_quad,
    whiten,
)


def chart(labels=True, menu=True):
    a = np.full((600, 900, 3), 242, np.uint8)
    cv2.rectangle(a, (100, 80), (680, 490), (35, 35, 35), 2)
    for x in range(150, 680, 60):
        cv2.line(a, (x, 81), (x, 489), (210, 210, 210), 1)
    for y in range(130, 490, 60):
        cv2.line(a, (101, y), (679, y), (210, 210, 210), 1)
    cv2.polylines(
        a, [np.int32([[110, 400], [230, 330], [400, 380], [660, 150]])], False, (20, 70, 210), 2
    )
    if labels:
        for y in range(100, 490, 60):
            cv2.putText(a, "100", (55, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 30, 30), 1)
        for x in range(130, 670, 70):
            cv2.putText(a, "20", (x, 520), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 30, 30), 1)
    if menu:
        cv2.rectangle(a, (10, 10), (880, 580), (60, 60, 60), 2)
        for y in range(80, 500, 70):
            cv2.rectangle(a, (720, y), (860, y + 40), (80, 80, 80), 2)
            cv2.putText(a, "MENU", (730, y + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 1)
    return a


def test_detection_menu_not_largest():
    q, evidence = detect(chart())
    assert q is not None, evidence
    assert (
        np.max(
            np.linalg.norm(q - np.float32([[100, 80], [680, 80], [680, 490], [100, 490]]), axis=1)
        )
        < 15
    )


@pytest.mark.parametrize(
    "q",
    [
        [[1, 1], [100, 100], [100, 1], [1, 100]],
        [[1, 1]] * 4,
        [[-1, 1], [100, 1], [100, 100], [1, 100]],
        [[1, 1], [100, 1], [100, float("nan")], [1, 100]],
    ],
)
def test_bad_quad(q):
    with pytest.raises(ChartError):
        validate_quad(q, (600, 900, 3))


def test_perspective_mapping():
    q = np.float32([[130, 80], [680, 120], [650, 490], [100, 450]])
    H, inverse, _, size = geometry(q)
    p = cv2.perspectiveTransform(q[None], H)[0]
    assert abs(p[0, 1] - p[1, 1]) < 1e-3
    assert abs(p[0, 0] - p[3, 0]) < 1e-3
    assert np.max(np.abs(cv2.perspectiveTransform(p[None], inverse)[0] - q)) < 1e-3
    assert size[0] * size[1] < 600 * 900


def test_conservative_whiten_and_sharpen():
    a = chart()
    b = whiten(a)
    color = (a.max(axis=2).astype(int) - a.min(axis=2)) > 60
    assert np.array_equal(a[color], b[color])
    assert b[150, 150, 0] < b[150, 151, 0] - 15  # faint grid contrast survives
    c = sharpen(b)
    assert np.max(np.abs(c.astype(int) - b)) <= 6
    assert np.array_equal(sharpen(b, 0), b)
    assert np.array_equal(sharpen(b, 10), sharpen(b, 1))


def test_no_overwrite_and_bad_input(tmp_path):
    # pytest's base directory is configured inside this project at invocation.
    p = tmp_path / "in.png"
    Image.fromarray(chart()).save(p)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    q = [[100, 80], [680, 80], [680, 490], [100, 490]]
    r1, r2 = [process(p, tmp_path, q) for _ in range(2)]
    assert r1["output"] != r2["output"]
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
    assert json.loads(open(r1["metadata"]).read())["stage"] == "chart-enhance"
    p.write_text("bad")
    with pytest.raises(ChartError):
        load_image(p)


def test_blank_requires_manual(tmp_path):
    a = np.full((300, 400, 3), 250, np.uint8)
    assert detect(a)[0] is None
    p = tmp_path / "blank.png"
    Image.fromarray(a).save(p)
    with pytest.raises(ChartError):
        process(p, tmp_path)
    assert not list(tmp_path.glob("*/final.png"))
    r = process(p, tmp_path, [[40, 40], [350, 40], [350, 250], [40, 250]])
    assert r["status"] == "ok"


def test_conflict_keeps_strongest_plot_not_exterior():
    from chart_enhance.core import select_candidate

    small = dict(
        score=0.986,
        area=0.46,
        corners_small=np.float32([[100, 100], [400, 100], [400, 400], [100, 400]]),
    )
    large = dict(
        score=0.977,
        area=0.68,
        corners_small=np.float32([[10, 10], [500, 10], [500, 500], [10, 500]]),
    )
    unrelated = dict(score=0.8, area=0.9, corners_small=large["corners_small"])
    selected, conflict = select_candidate([small, large, unrelated])
    assert conflict and selected is small
    weak = dict(score=0.69, area=0.95, corners_small=large["corners_small"])
    selected, conflict = select_candidate([small, weak])
    assert selected is small and not conflict


def test_supported_interior_rails_not_outer_menu():
    from chart_enhance.core import refine_boundary

    outer = np.float32([[20, 50], [480, 50], [480, 560], [20, 560]])
    frame = np.float32([[100, 140], [400, 140], [400, 500], [100, 500]])
    edges = np.zeros((600, 500), np.uint8)
    cv2.rectangle(edges, (100, 70), (400, 510), 255, 3)
    horizontal = [(300, (100, 70, 400, 70)), (300, (100, 510, 400, 510))]
    q = refine_boundary(outer, [frame], horizontal, [(400, (400, 70, 400, 510))], edges)
    assert np.allclose(q, [[100, 140], [400, 140], [400, 510], [100, 510]])
    edges[:, 398:403] = 0
    assert refine_boundary(outer, [frame], horizontal, [(400, (400, 70, 400, 510))], edges) is None



def test_no_coordinates_means_exact_quad_crop(tmp_path):
    source = tmp_path / "bare.png"
    Image.fromarray(chart(False, False)).save(source)
    q = np.float32([[100, 80], [680, 80], [680, 490], [100, 490]])
    result = process(source, tmp_path / "out", corners=q)
    assert result["settings"]["margins"] == [0, 0, 0, 0]
    assert result["settings"]["coordinate_padding"] == [0, 0, 0, 0]
    assert np.allclose(result["crop_corners"], q)


def test_repeated_grid_extends_both_sides_not_menu():
    from chart_enhance.core import grid_boundary

    gray = np.full((800, 1100), 235, np.uint8)
    for y in range(100, 701, 50):
        cv2.line(gray, (100, y), (850, y), 80, 2)
    for x in range(100, 851, 75):
        cv2.line(gray, (x, 100), (x, 700), 80, 2)
    for y in range(100, 701, 30):
        cv2.rectangle(gray, (910, y), (1080, y + 18), 70, 1)
    inner = np.float32([[250, 200], [700, 200], [700, 700], [250, 700]])
    # Allow endpoint search around a plausible inner rail, not arbitrary screen size.
    inner[:, 0] = [200, 775, 775, 200]
    q, evidence = grid_boundary(inner, gray)
    assert q is not None, evidence
    assert evidence["repeated_rows"] >= 10
    assert np.max(np.abs(q - [[100, 100], [850, 100], [850, 700], [100, 700]])) < 5
