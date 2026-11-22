import json
from pathlib import Path

import pytest
from PIL import Image

from image_preprocess.cli import main
from image_preprocess.core import preprocess


def test_resize_and_preserve_source(tmp_path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (3000, 1500), (140, 70, 20)).save(source)
    before = source.read_bytes()
    result = preprocess(source, tmp_path / "output")
    assert result["output_size"] == [2048, 1024]
    assert result["resized"]
    with Image.open(result["output"]) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        assert image.size == (2048, 1024)
    assert source.read_bytes() == before


def test_small_grayscale_and_collision(tmp_path):
    source = tmp_path / "gray.png"
    Image.new("L", (20, 10), 120).save(source)
    a = preprocess(source, tmp_path)
    b = preprocess(source, tmp_path)
    assert a["output"] != b["output"] != str(source)
    assert a["output_size"] == [20, 10] and not a["resized"]
    assert Image.open(a["output"]).mode == "RGB"


def test_alpha_white_background(tmp_path):
    source = tmp_path / "alpha.png"
    Image.new("RGBA", (3, 1), (0, 0, 0, 0)).save(source)
    result = preprocess(source, tmp_path / "output")
    with Image.open(result["output"]) as im:
        assert im.getpixel((0, 0)) == (255, 255, 255)


def test_exif_orientation_and_metadata_removed(tmp_path):
    source = tmp_path / "oriented.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (30, 10)).save(source, exif=exif)
    result = preprocess(source, tmp_path / "output")
    assert result["original_size"] == [10, 30]
    with Image.open(result["output"]) as image:
        assert image.size == (10, 30) and not image.getexif()


def test_batch_json_and_partial_failure(tmp_path, capsys):
    source = tmp_path / "input"
    source.mkdir()
    Image.new("RGB", (12, 13)).save(source / "b.png")
    (source / "a.jpg").write_bytes(b"broken")
    (source / "ignored.txt").write_text("ignored")
    assert main([str(source), "--output", str(tmp_path / "output")]) == 1
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert [row["status"] for row in rows] == ["error", "ok"]
    assert all(row["protocol_version"] == 1 and row["stage"] == "preprocess" for row in rows)
    assert all(Path(row["input"]).is_absolute() for row in rows)
    assert Path(rows[1]["output"]).is_absolute()
    assert rows[1]["output_size"] == [12, 13]
    assert "1 成功，1 失败" in captured.err


def test_animated_and_high_bit_depth_rejected(tmp_path):
    source = tmp_path / "animated.png"
    Image.new("RGB", (4, 4), "red").save(
        source, save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")]
    )
    with pytest.raises(ValueError, match="动画"):
        preprocess(source, tmp_path / "output")
    source = tmp_path / "depth.png"
    Image.new("I;16", (4, 4)).save(source)
    with pytest.raises(ValueError):
        preprocess(source, tmp_path / "output")


@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_size(tmp_path, value):
    with pytest.raises(ValueError):
        preprocess(tmp_path / "missing.png", tmp_path / "out", value)


def test_directory_cannot_output_to_itself(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--output", str(tmp_path)])
