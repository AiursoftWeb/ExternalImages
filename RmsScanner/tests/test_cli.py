"""CLI preflight must finish before invoking any image stages."""
from unittest.mock import patch

import pytest
from PIL import Image

import scan


@pytest.mark.parametrize("layout", ["same-directory", "resolved-alias", "batch-overlap"])
def test_cli_rejects_output_overlapping_input(tmp_path, layout):
    source = tmp_path / "source"
    source.mkdir()
    image = source / "plot.png"
    Image.new("RGB", (128, 128), "white").save(image)
    output = source
    input_path = image
    if layout == "resolved-alias":
        output = tmp_path / "alias"
        output.symlink_to(source, target_is_directory=True)
    elif layout == "batch-overlap":
        Image.new("RGB", (128, 128), "black").save(source / "aaa.jpg")
        input_path = source
    originals = {path: path.read_bytes() for path in source.iterdir()}
    with patch("scan.process") as process, pytest.raises(SystemExit) as error:
        scan.main([str(input_path), "-o", str(output)])
    assert error.value.code == 2
    process.assert_not_called()
    assert {path: path.read_bytes() for path in source.iterdir()} == originals


def test_cli_rejects_duplicate_stems_before_any_processing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("aaa.png", "plot.jpg", "plot.png"):
        Image.new("RGB", (128, 128)).save(source / name)
    output = tmp_path / "output"
    with patch("scan.process") as process, pytest.raises(SystemExit) as error:
        scan.main([str(source), "-o", str(output)])
    assert error.value.code == 2
    process.assert_not_called()
    assert not output.exists()


@pytest.mark.parametrize("options", [[], ["--strength", "30", "--max-side", "8192"],
                                   ["--strength", "0.001", "--max-side", "128", "--threads", "1"]])
def test_cli_accepts_legacy_ranges_and_thread_default(tmp_path, monkeypatch, options):
    monkeypatch.setenv("OPENCV_THREADS", "2")
    source = tmp_path / "plot.jpg"
    Image.new("RGB", (128, 128)).save(source)
    with patch("scan.process") as process:
        assert scan.main([str(source), "-o", str(tmp_path / "output"), *options]) == 0
    process.assert_called_once()
    args = process.call_args.args[2]
    assert args.threads == (1 if "--threads" in options else 4)
    assert scan.os.environ["OPENCV_THREADS"] == str(args.threads)


@pytest.mark.parametrize("options", [["--strength", value] for value in ("0", "-1", "nan", "inf", "-inf")]
                         + [["--max-side", "127"], ["--threads", "0"], ["--passes", "3"],
                            ["--corners", "[]", "--no-crop"]])
def test_cli_rejects_invalid_options_before_processing(tmp_path, options):
    source = tmp_path / "plot.png"
    Image.new("RGB", (128, 128)).save(source)
    with patch("scan.process") as process, pytest.raises(SystemExit) as error:
        scan.main([str(source), "-o", str(tmp_path / "output"), *options])
    assert error.value.code == 2
    process.assert_not_called()
