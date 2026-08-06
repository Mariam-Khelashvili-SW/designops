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
    assert "<strong>What we aligned on</strong>" in html
    assert "<ul>" in html
    assert "<li>First item</li>" in html
    assert "<li>Second <strong>bold</strong> item</li>" in html
    assert "<p>Thanks for joining.</p>" in html
    assert "<p>Best regards</p>" in html
    assert "**" not in html
    assert "<script>" not in html


def test_escapes_html():
    html = markdown_lite_to_html('Hello <script>alert(1)</script> **x**')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>x</strong>" in html


def test_markdown_link():
    html = markdown_lite_to_html("See [Figma](https://www.figma.com/file/abc)")
    assert 'href="https://www.figma.com/file/abc"' in html
    assert ">Figma</a>" in html


def test_plain_strips_markers():
    assert markdown_lite_to_plain("**Hello** world") == "Hello world"
