"""slugify() — regression cover for the Webflow-invalid-slug fix (#6).

Before the fix, str.isalnum() let Unicode letters/digits through, so
generated-page exports produced slugs Webflow rejects and all-Unicode
input never reached the caller's fallback.
"""

import pytest

app_module = pytest.importorskip("app")
slugify = app_module.slugify


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello World", "hello-world"),
        ("AI Visibility Service Page", "ai-visibility-service-page"),
        # Accented Latin folds to ASCII rather than being dropped.
        ("Café & Crème Brûlée", "cafe-creme-brulee"),
        ("Naïve Façade — Test", "naive-facade-test"),
        ("  Multiple   Spaces  ", "multiple-spaces"),
        ("under_score-and-dash", "under-score-and-dash"),
        ("Trailing!!!", "trailing"),
        # Non-ASCII scripts drop out entirely -> empty -> caller fallback.
        ("日本語", ""),
        ("北京 SEO 2025", "seo-2025"),
        # Unicode (Arabic-Indic) digits are not URL-safe -> dropped.
        ("٤٢ widgets", "widgets"),
        ("", ""),
        (None, ""),
    ],
)
def test_slugify_cases(raw, expected):
    assert slugify(raw) == expected


def test_output_is_ascii_url_safe():
    out = slugify("Ünïcödé Tëst Pàge 2025")
    assert out == out.encode("ascii", "ignore").decode()
    assert all(c.islower() or c.isdigit() or c == "-" for c in out)


def test_idempotent():
    once = slugify("Some Mixed Title 2025")
    assert slugify(once) == once
