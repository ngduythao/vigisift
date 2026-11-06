from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "vigisift" / "web" / "index.html"


def test_the_demo_build_can_still_find_where_to_inject_its_script() -> None:
    # scripts/demo_site.py puts the demo banner after <body> and its fetch shim before the
    # page's one inline script; a second of either would leave the demo talking to no server.
    page = PAGE.read_text()
    assert page.count("<body>") == 1
    assert page.count("<script>") == 1
