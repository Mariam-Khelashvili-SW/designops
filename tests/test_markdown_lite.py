from designops.api.markdown_lite import markdown_lite_to_html, markdown_lite_to_plain


def test_bold_and_lists():
    src = (
        "Thanks for joining.\n\n"
        "**What we aligned on**\n"
        "- First item\n"
        "- Second **bold** item\n\n"
        "Best regards"
    )
    html = markdown_lite_to_html(src)
    assert "<strong" in html and "What we aligned on</strong>" in html
    assert 'style="' in html
    assert "<ul" in html
    assert "<li" in html
    assert "First item" in html
    assert "Second <strong" in html and "bold</strong>" in html
    assert "<p" in html
    assert "Thanks for joining." in html
    assert "Best regards" in html
    assert "**" not in html
    assert "<script>" not in html


def test_asterisk_bullets():
    src = "As next steps from your side, please:\n* First\n* Second"
    html = markdown_lite_to_html(src)
    assert html.count("<li") == 2
    plain = markdown_lite_to_plain(src)
    assert "• First" in plain
    assert "• Second" in plain


def test_escapes_html():
    html = markdown_lite_to_html('Hello <script>alert(1)</script> **x**')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong" in html and ">x</strong>" in html


def test_markdown_link():
    html = markdown_lite_to_html("See [Figma](https://www.figma.com/file/abc)")
    assert 'href="https://www.figma.com/file/abc"' in html
    assert ">Figma</a>" in html


def test_plain_strips_markers():
    assert markdown_lite_to_plain("**Hello** world") == "Hello world"
    assert markdown_lite_to_plain("- one\n- two") == "• one\n• two"
