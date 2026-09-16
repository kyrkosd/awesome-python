"""Tests for the build module."""

import json
import os
import shutil
import textwrap
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from build import (
    TemplateEntry,
    annotate_entries_with_stats,
    build,
    detect_source_type,
    extract_entries,
    extract_github_repo,
    load_downloads,
    load_pypi_badges,
    load_stars,
    sort_entries,
    subcategory_path,
)
from readme_parser import parse_readme, slugify


class HeadMetadataParser(HTMLParser):
    """Parse HTML head metadata for testing purposes."""

    def __init__(self):
        super().__init__()
        self.title_count = 0
        self.title = ""
        self.meta_by_name = {}
        self.meta_by_property = {}
        self.links_by_rel = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        """Handle start tags to extract metadata."""
        attrs = dict(attrs)
        if tag == "title":
            self.title_count += 1
            self._in_title = True
        elif tag == "meta":
            if "name" in attrs:
                self.meta_by_name[attrs["name"]] = attrs.get("content", "")
            if "property" in attrs:
                self.meta_by_property[attrs["property"]] = attrs.get("content", "")
        elif tag == "link" and attrs.get("rel"):
            for rel in attrs["rel"].split():
                self.links_by_rel[rel] = attrs.get("href", "")

    def handle_endtag(self, tag):
        """Handle end tags."""
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        """Handle data inside title tags."""
        if self._in_title:
            self.title += data


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    """Tests for the slugify function."""

    def test_simple(self):
        """Test simple slugification."""
        assert slugify("Admin Panels") == "admin-panels"

    def test_uppercase_acronym(self):
        """Test slugification of uppercase acronyms."""
        assert slugify("RESTful API") == "restful-api"

    def test_hyphenated_input(self):
        """Test slugification with hyphenated input."""
        assert slugify("Command-line Tools") == "command-line-tools"

    def test_extra_spaces(self):
        """Test slugification with extra spaces."""
        assert slugify("  Date  and  Time  ") == "date-and-time"


class TestSubcategoryPath:
    """Tests for the subcategory_path function."""

    def test_builds_path(self):
        """Test that subcategory_path builds the correct path."""
        assert subcategory_path(
            "web-frameworks", "synchronous"
        ) == "/categories/web-frameworks/synchronous/"


# ---------------------------------------------------------------------------
# build (integration)
# ---------------------------------------------------------------------------


class TestBuild:
    """Integration tests for the build function."""

    @pytest.fixture(autouse=True)
    def _make_sponsorship_md(self, tmp_path):
        """Create a sponsorship file for tests."""
        (tmp_path / "SPONSORSHIP.md").write_text(
            "# Sponsorship\n", encoding="utf-8"
        )

    def _make_repo(self, tmp_path, readme):
        """Create a minimal repo structure for testing."""
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        tpl_dir = tmp_path / "website" / "templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "base.html").write_text(
            "<!DOCTYPE html><html lang='en'><head>"
            "<title>{% block title %}{% endblock %}</title>"
            "<meta name='description' "
            "content='{% block description %}{% endblock %}'>"
            "</head><body>{% block content %}{% endblock %}</body></html>",
            encoding="utf-8",
        )
        (tpl_dir / "index.html").write_text(
            '{% extends "base.html %}{% block content %}'
            "{% for entry in entries %}"
            '<div class="row">'
            "<span>{{ entry.name }}</span>"
            "<span>{{ entry.categories | join(', ') }}</span>"
            "<span>{{ entry.groups | join(', ') }}</span>"
            "</div>"
            "{% endfor %}"
            "{% endblock %}",
            encoding="utf-8",
        )
        (tpl_dir / "category.html").write_text(
            '{% extends "base.html %}{% block content %}'
            "<h1>{{ category.name }}</h1>"
            "{% for entry in entries %}"
            '<a href="{{ entry.url }}">{{ entry.name }}</a>'
            "{% endfor %}{% endblock %}",
            encoding="utf-8",
        )
        (tpl_dir / "sponsorship.html").write_text(
            '{% extends "base.html %}{% block content %}'
            "<h1>Sponsor</h1>{% endblock %}",
            encoding="utf-8",
        )
        (tpl_dir / "llms.txt").write_text(
            "# Awesome Python\n\nHomepage: {{ site_url }}\n\n"
            "## Categories\n\n{{ categories_md }}\n",
            encoding="utf-8",
        )

    def _copy_real_templates(self, tmp_path):
        """Copy real templates from the source directory."""
        real_tpl = Path(__file__).parent / ".." / "templates"
        tpl_dir = tmp_path / "website" / "templates"
        shutil.copytree(real_tpl, tpl_dir)

    def test_build_creates_homepage_and_category_pages(self, tmp_path):
        """Test that build creates homepage and category pages."""
        readme = textwrap.dedent("""\
            # Awesome Python

            Intro.

            ## Projects

            **Tools**

            ### Widgets

            _Widget libraries. Also see [awesome-widgets](https://example.com/widgets)._

            - [w1](https://example.com) - A widget.

            ### Gadgets

            _Gadget tools._

            - [g1](https://example.com) - A gadget.

            ## Resources

            Info.

            ### Newsletters

            - [NL](https://example.com)

            ## Contributing

            Help!
        """)
        self._make_repo(tmp_path, readme)
        build(tmp_path)

        site = tmp_path / "website" / "output"
        assert (site / "index.html").exists()
        assert (site / "categories" / "widgets" / "index.html").exists()
        assert (site / "categories" / "gadgets" / "index.html").exists()

    def test_build_creates_root_discovery_files(self, tmp_path):
        """Test that build creates robots.txt and sitemap.xml."""
        readme = textwrap.dedent("""\
            # Awesome Python

            Intro.

            ## Projects

            **Tools**

            ### Widgets

            - Sync

                - [w1](https://example.com) - A widget.

            ## Contributing

            Help!
        """)
        self._make_repo(tmp_path, readme)
        sponsorship_mtime = datetime(2024, 1, 2, tzinfo=UTC).timestamp()
        os.utime(tmp_path / "SPONSORSHIP.md", (sponsorship_mtime, sponsorship_mtime))
        expected_sponsorship_lastmod = "2024-01-02"
        start_date = datetime.now(UTC).date()
        build(tmp_path)
        end_date = datetime.now(UTC).date()

        site = tmp_path / "website" / "output"
        robots = (site / "robots.txt").read_text(encoding="utf-8")
        expected_robots = (
            "User-agent: *\n"
            "Content-Signal: search=yes, "
            "ai-input=yes, ai-train=yes\n"
            "Allow: /\n\n"
            "Sitemap: https://awesome-python.com/sitemap.xml\n"
        )
        assert robots == expected_robots

        sitemap = ET.parse(site / "sitemap.xml")
        root = sitemap.getroot()
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [
            loc.text or ""
            for loc in root.findall("sitemap:url/sitemap:loc", ns)
        ]
        lastmods = [
            lastmod.text or ""
            for lastmod in root.findall(
                "sitemap:url/sitemap:lastmod", ns
            )
        ]
        lastmod_by_loc = dict(zip(locs, lastmods, strict=True))

        expected_ns_tag = "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset"
        assert root.tag == expected_ns_tag
        assert locs == [
            "https://awesome-python.com/",
            "https://awesome-python.com/categories/widgets/",
            "https://awesome-python.com/categories/tools/",
            "https://awesome-python.com/categories/widgets/sync/",
            "https://awesome-python.com/sponsorship/",
        ]
        assert len(lastmods) == len(locs)
        assert lastmod_by_loc[
            "https://awesome-python.com/sponsorship/"
        ] == expected_sponsorship_lastmod
        for loc, lastmod in lastmod_by_loc.items():
            if loc != "https://awesome-python.com/sponsorship/":
                assert start_date <= date.fromisoformat(
                    lastmod
                ) <= end_date
        assert all(
            loc.startswith("https://awesome-python.com/") for loc in locs
        )
        assert all("?" not in loc for loc in locs)

    def test_build_creates_category_pages_with_metadata_and_links(
        self, tmp_path
    ):
        """Test that category pages contain correct metadata and links."""
        readme = textwrap.dedent("""\
            # Awesome Python

            Intro.

            ## Projects

            **Tools**

            ## Widgets

            _Widget libraries. Also see [awesome-widgets](https://example.com/widgets)._

            - [w1](https://example.com/w1) - A widget.
            - [w2](https://github.com/owner/w2) - A starred widget.

            ## Gadgets

            _Gadget tools._

            - [g1](https://example.com/g1) - A gadget.

            # Contributing

            Help!
        """)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)

        data_dir = tmp_path / "website" / "data"
        data_dir.mkdir(parents=True)
        stars = {
            "owner/w2": {
                "stars": 42,
                "owner": "owner",
                "last_commit_at": "2026-01-01T00:00:00+00:00",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
        }
        (data_dir / "github_stars.json").write_text(
            json.dumps(stars), encoding="utf-8"
        )

        build(tmp_path)

        site = tmp_path / "website" / "output"
        index_html = (site / "index.html").read_text(encoding="utf-8")
        category_html = (
            site / "categories" / "widgets" / "index.html"
        ).read_text(encoding="utf-8")
        parser = HeadMetadataParser()
        parser.feed(category_html)

        assert 'href="/categories/widgets/"' in index_html
        assert 'data-value="Widgets"' in index_html
        assert parser.title.strip() == "Widgets Python Libraries - Awesome Python"
        expected_desc = (
            "Widget libraries. Also see awesome-widgets. "
            "Explore 2 curated Python projects in Widgets."
        )
        assert parser.meta_by_name["description"] == expected_desc
        assert (
            parser.links_by_rel["canonical"]
            == "https://awesome-python.com/categories/widgets/"
        )
        assert (
            parser.meta_by_property["og:url"]
            == "https://awesome-python.com/categories/widgets/"
        )
        llms_link = (
            '<link rel="alternate" type="text/plain" '
            'href="/llms.txt" title="LLMs text entry point" />'
        )
        assert llms_link not in category_html
        assert (
            '<a href="/sponsorship/" class="hero-topbar-link">'
            "Sponsorship</a>" in category_html
        )
        assert "<h1>Widgets</h1>" in category_html
        expected_awesome_link = (
            'Widget libraries. Also see '
            '<a href="https://example.com/widgets" '
            'target="_blank" rel="noopener">awesome-widgets</a>.'
        )
        assert expected_awesome_link in category_html
        assert 'href="https://example.com/w1"' in category_html
        assert "A widget." in category_html
        assert 'href="https://github.com/owner/w2"' in category_html
        assert '<table class="table">' in category_html
        assert "42" in category_html
        assert "2026-01-01T00:00:00+00:00" in category_html

    def test_build_creates_llms_text_alternate_without_sponsors(
        self, tmp_path
    ):
        """Test that LLMs.txt alternate is created correctly without sponsors section."""
        readme = textwrap.dedent("""\
            # Awesome Python

            Intro.

            ## **Sponsors**

            - **[Sponsor](https://sponsor.example.com)**: Sponsored tool.

            > Become a sponsor: [Sponsor us](SPONSORSHIP.md).

            ## Categories

            **Tools**

            - [Widgets](#widgets)

            ## Projects

            **Tools**

            ### Widgets

            - [w1](https://example.com) - A widget.
            - [w2](https://github.com/owner/w2) - A starred widget.

            ## Contributing

            Help!
        """)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)

        data_dir = tmp_path / "website" / "data"
        data_dir.mkdir(parents=True)
        stars = {
            "owner/w2": {
                "stars": 42,
                "owner": "owner",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
        }
        (data_dir / "github_stars.json").write_text(
            json.dumps(stars), encoding="utf-8"
        )
        (data_dir / "pypi_downloads.tsv").write_text(
            "name\tpackage\tdownloads\tfetched_at\n"
            "w1\tw1\t777\t2026-08-16\n",
            encoding="utf-8",
        )

        build(tmp_path)

        site = tmp_path / "website" / "output"
        index_html = (site / "index.html").read_text(encoding="utf-8")
        llms_txt = (site / "llms.txt").read_text(encoding="utf-8")

        expected_llms_link = (
            '<link rel="alternate" type="text/plain" '
            'href="/llms.txt" title="LLMs text entry point" />'
        )
        assert expected_llms_link in index_html

        assert llms_txt.startswith("# Awesome Python\n\nIntro.\n")
        assert "2 projects across 1 category, updated on " in llms_txt
        assert "Scan the category index" in llms_txt
        assert "Homepage: https://awesome-python.com/" in llms_txt
        assert "Markdown homepage" not in llms_txt
        assert "https://awesome-python.com/index.md" not in llms_txt
        assert (
            "GitHub repository: "
            "https://github.com/vinta/awesome-python"
        ) in llms_txt
        assert (
            "Contributing guide: "
            "https://github.com/vinta/awesome-python/blob/master/CONTRIBUTING.md"
        ) in llms_txt
        assert "Sponsorship: https://awesome-python.com/sponsorship/" in llms_txt
        assert "Sitemap: https://awesome-python.com/sitemap.xml" in llms_txt
        assert "## Categories" in llms_txt
        assert "**Tools**" in llms_txt
        assert (
            "- [Widgets](https://awesome-python.com/categories/widgets/)"
            in llms_txt
        )
        assert "- [Widgets](#widgets)" not in llms_txt
        assert "### Widgets" in llms_txt
        expected_w1 = (
            "- [w1](https://example.com) - A widget. "
            "(PyPI downloads/month: 777)"
        )
        assert expected_w1 in llms_txt
        expected_w2 = (
            "- [w2](https://github.com/owner/w2) - A starred widget. "
            "(GitHub stars: 42)"
        )
        assert expected_w2 in llms_txt
        assert llms_txt != readme
        assert "# Contributing" not in llms_txt

    def test_build_cleans_stale_output(self, tmp_path):
        """Test that build removes stale output files."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            ## Only

            - [x](https://x.com) - X.

            # Contributing

            Done.
        """)
        self._make_repo(tmp_path, readme)

        stale = tmp_path / "website" / "output" / "categories" / "stale"
        stale.mkdir(parents=True)
        (stale / "index.html").write_text("old", encoding="utf-8")

        build(tmp_path)

        assert not (
            tmp_path / "website" / "output" / "categories" / "stale"
        ).exists()

    def test_build_with_stars_sorts_by_stars(self, tmp_path):
        """Test that entries with stars are sorted by star count."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            ## Stuff

            - [low-stars](https://github.com/org/low) - Low.
            - [high-stars](https://github.com/org/high) - High.
            - [no-stars](https://example.com/none) - None.

            # Contributing

            Done.
        """)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")

        # Copy real templates
        real_tpl = Path(__file__).parent / ".." / "templates"
        tpl_dir = tmp_path / "website" / "templates"
        shutil.copytree(real_tpl, tpl_dir)

        # Create mock star data
        data_dir = tmp_path / "website" / "data"
        data_dir.mkdir(parents=True)
        stars = {
            "org/high": {
                "stars": 5000,
                "owner": "org",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
            "org/low": {
                "stars": 100,
                "owner": "org",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
        }
        (data_dir / "github_stars.json").write_text(
            json.dumps(stars), encoding="utf-8"
        )

        build(tmp_path)

        html = (
            tmp_path / "website" / "output" / "index.html"
        ).read_text(encoding="utf-8")
        # Star-sorted: high-stars (5000) before low-stars (100) before no-stars (None)
        assert html.index("high-stars") < html.index("low-stars")
        assert html.index("low-stars") < html.index("no-stars")
        # Formatted star counts
        assert "5,000" in html
        assert "100" in html
        # Expand content present
        assert "expand-content" in html

    def test_build_with_downloads_renders_column(self, tmp_path):
        """Test that PyPI downloads are rendered correctly."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            ## Stuff

            - [My-Lib](https://github.com/org/mylib) - On PyPI.
            - [no-pypi](https://example.com/none) - Not on PyPI.
            - [asyncio](https://docs.python.org/3/library/asyncio.html) - Stdlib.
            - [my-lib.thing](https://github.com/org/mylib) - (part of My-Lib) A bundled feature.

            # Contributing

            Done.
        """)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)

        data_dir = tmp_path / "website" / "data"
        data_dir.mkdir(parents=True)
        # Keyed by normalized README display name, like fetch_pypi_downloads_via_clickpy.py writes it
        (data_dir / "pypi_downloads.tsv").write_text(
            "name\tpackage\tdownloads\tfetched_at\n"
            "asyncio\tasyncio\t26305454\t2026-08-16\n"
            "my-lib\tmy-lib\t1234567\t2026-08-16\n",
            encoding="utf-8",
        )

        build(tmp_path)

        html = (
            tmp_path / "website" / "output" / "index.html"
        ).read_text(encoding="utf-8")
        assert "1,234,567" in html
        # Stdlib entries never show PyPI counts: the asyncio row is the backport package
        assert "26,305,454" not in html
        # Default sort: entries with download counts come first
        assert html.index("My-Lib") < html.index("no-pypi")
        # Each no-download entry gets the badge matching why it has no count
        assert html.count(
            '<span class="source-badge">Stdlib</span>'
        ) == 1
        assert html.count(
            '<span class="source-badge">Bundled</span>'
        ) == 1
        assert html.count(
            '<span class="source-badge">Not on PyPI</span>'
        ) == 1

    def test_build_fails_when_group_and_category_slug_collide(
        self, tmp_path
    ):
        """Test that build raises ValueError when slugs collide."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Widgets**

            ## Widgets

            - [w1](https://example.com) - W.

            # Contributing

            Done.
        """)
        self._make_repo(tmp_path, readme)
        with pytest.raises(ValueError, match="slug collision"):
            build(tmp_path)

    def test_index_contains_aligned_homepage_metadata(self, tmp_path):
        """Test that index page contains correct metadata."""
        readme = (
            Path(__file__).parents[2] / "README.md"
        ).read_text(encoding="utf-8")
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)

        build(tmp_path)

        parsed_groups = parse_readme(readme)
        categories = [
            cat
            for group in parsed_groups
            for cat in group["categories"]
        ]
        entries = extract_entries(categories, parsed_groups)
        html = (
            tmp_path / "website" / "output" / "index.html"
        ).read_text(encoding="utf-8")
        parser = HeadMetadataParser()
        parser.feed(html)

        expected_title = "Awesome Python"
        expected_description = (
            f"An opinionated guide to the best Python frameworks, "
            f"libraries, and tools. Explore {len(entries)} curated "
            f"projects across {len(categories)} categories, from "
            f"AI and agents to data science and web development."
        )
        expected_url = "https://awesome-python.com/"
        expected_image = (
            "https://awesome-python.com/static/og-image.png"
        )

        assert parser.title_count == 1
        assert parser.title.strip() == expected_title
        assert parser.meta_by_name["description"] == expected_description
        assert parser.links_by_rel["canonical"] == expected_url
        assert parser.meta_by_property["og:type"] == "website"
        assert parser.meta_by_property["og:title"] == expected_title
        assert (
            parser.meta_by_property["og:description"] == expected_description
        )
        assert parser.meta_by_property["og:image"] == expected_image
        assert parser.meta_by_property["og:url"] == expected_url
        assert (
            parser.meta_by_name["twitter:card"] == "summary_large_image"
        )
        assert (
            parser.meta_by_name["twitter:title"] == expected_title
        )
        assert (
            parser.meta_by_name["twitter:description"]
            == expected_description
        )
        assert (
            parser.meta_by_name["twitter:image"] == expected_image
        )
        assert "<head>\n    <meta charset" in html
        assert (
            '<a href="/sponsorship/" class="hero-topbar-link">'
            "Sponsorship</a>" in html
        )
        assert (
            'id="hero-category-heading">Browse by category</h2>'
            in html
        )
        assert (
            'class="hero-category-link" '
            'href="/categories/ai-and-agents/"' in html
        )

    def test_index_contains_homepage_json_ld(self, tmp_path):
        """Test that index page contains correct JSON-LD."""
        readme = (
            Path(__file__).parents[2] / "README.md"
        ).read_text(encoding="utf-8")
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)

        build(tmp_path)

        parsed_groups = parse_readme(readme)
        categories = [
            cat
            for group in parsed_groups
            for cat in group["categories"]
        ]
        entries = extract_entries(categories, parsed_groups)
        html = (
            tmp_path / "website" / "output" / "index.html"
        ).read_text(encoding="utf-8")

        marker = '<script type="application/ld+json">'
        assert marker in html
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        block = html[start:end]
        assert "</script>" not in block
        data = json.loads(block)

        assert data["@context"] == "https://schema.org"
        graph = {node["@type"]: node for node in data["@graph"]}
        assert set(graph) == {"WebSite", "CollectionPage"}
        assert graph["WebSite"]["url"] == "https://awesome-python.com/"
        assert graph["WebSite"]["name"] == "Awesome Python"
        assert (
            graph["WebSite"]["@id"]
            == "https://awesome-python.com/#website"
        )

        collection = graph["CollectionPage"]
        assert collection["@id"] == "https://awesome-python.com/"
        assert collection["url"] == "https://awesome-python.com/"
        assert collection["isPartOf"] == {
            "@type": "WebSite",
            "@id": graph["WebSite"]["@id"],
        }
        expected_description = (
            f"An opinionated guide to the best Python frameworks, "
            f"libraries, and tools. Explore {len(entries)} curated "
            f"projects across {len(categories)} categories, from "
            f"AI and agents to data science and web development."
        )
        assert collection["description"] == expected_description

        item_list = collection["mainEntity"]
        assert item_list["@type"] == "ItemList"
        assert item_list["numberOfItems"] == len(entries)
        assert len(item_list["itemListElement"]) == len(entries)

        positions = [
            item["position"] for item in item_list["itemListElement"]
        ]
        assert positions == list(range(1, len(entries) + 1))
        assert all(
            item["@type"] == "ListItem"
            for item in item_list["itemListElement"]
        )
        assert all(
            item["url"].startswith(("http://", "https://"))
            for item in item_list["itemListElement"]
        )

        rendered_names = {
            item["name"] for item in item_list["itemListElement"]
        }
        rendered_urls = {
            item["url"] for item in item_list["itemListElement"]
        }
        assert rendered_names == {e["name"] for e in entries}
        assert rendered_urls == {e["url"] for e in entries}

    def test_category_page_contains_json_ld(self, tmp_path):
        """Test that category page contains correct JSON-LD."""
        readme = textwrap.dedent("""\
            # Awesome Python

            Intro.

            ## Projects

            **Tools**

            ## Widgets

            _Widget libraries._

            - [w1](https://example.com/w1) - A widget.
            - [w2](https://github.com/owner/w2) - A starred widget.

            # Contributing

            Help!
        """)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        self._copy_real_templates(tmp_path)
        build(tmp_path)

        category_html = (
            tmp_path / "website" / "output"
            / "categories" / "widgets" / "index.html"
        ).read_text(encoding="utf-8")
        marker = '<script type="application/ld+json">'
        assert marker in category_html
        start = category_html.index(marker) + len(marker)
        end = category_html.index("</script>", start)
        block = category_html[start:end]
        assert "</script>" not in block
        data = json.loads(block)

        assert data["@context"] == "https://schema.org"
        graph = {node["@type"]: node for node in data["@graph"]}
        assert set(graph) == {
            "WebSite",
            "CollectionPage",
            "BreadcrumbList",
        }
        assert (
            graph["WebSite"]["@id"]
            == "https://awesome-python.com/#website"
        )
        collection = graph["CollectionPage"]
        assert collection["name"] == "Widgets Python Libraries"
        assert (
            collection["@id"]
            == "https://awesome-python.com/categories/widgets/"
        )
        assert (
            collection["url"]
            == "https://awesome-python.com/categories/widgets/"
        )
        expected_desc = (
            "Widget libraries. Explore 2 curated Python "
            "projects in Widgets."
        )
        assert collection["description"] == expected_desc
        assert collection["isPartOf"] == {
            "@type": "WebSite",
            "@id": "https://awesome-python.com/#website",
        }

        item_list = collection["mainEntity"]
        assert item_list["@type"] == "ItemList"
        assert item_list["numberOfItems"] == 2
        names = {
            item["name"] for item in item_list["itemListElement"]
        }
        urls = {
            item["url"] for item in item_list["itemListElement"]
        }
        assert names == {"w1", "w2"}
        assert urls == {
            "https://example.com/w1",
            "https://github.com/owner/w2",
        }
        positions = sorted(
            item["position"]
            for item in item_list["itemListElement"]
        )
        assert positions == [1, 2]

        breadcrumbs = graph["BreadcrumbList"]["itemListElement"]
        expected_breadcrumbs = [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Awesome Python",
                "item": "https://awesome-python.com/",
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Widgets",
                "item": "https://awesome-python.com/categories/widgets/",
            },
        ]
        assert breadcrumbs == expected_breadcrumbs

    def test_group_page_falls_back_to_default_description_in_json_ld(
        self, tmp_path
    ):
        """Test that group page uses default description in JSON-LD."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **AI & ML**

            ## Deep Learning

            - [dl1](https://example.com/dl1) - DL.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        group_html = (
            tmp_path / "website" / "output"
            / "categories" / "ai-ml" / "index.html"
        ).read_text(encoding="utf-8")
        marker = '<script type="application/ld+json">'
        start = group_html.index(marker) + len(marker)
        end = group_html.index("</script>", start)
        data = json.loads(group_html[start:end])

        graph = {node["@type"]: node for node in data["@graph"]}
        collection = graph["CollectionPage"]
        assert collection["name"] == "AI & ML Python Libraries"
        assert (
            collection["@id"]
            == "https://awesome-python.com/categories/ai-ml/"
        )
        assert (
            collection["url"]
            == "https://awesome-python.com/categories/ai-ml/"
        )
        expected_desc = (
            "Explore 1 curated Python projects in AI & ML. "
            "Part of the Awesome Python catalog."
        )
        assert collection["description"] == expected_desc

    def test_build_creates_subcategory_pages(self, tmp_path):
        """Test that build creates subcategory pages."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Web**

            ## Web Frameworks

            - Synchronous

                - [django](https://example.com/django) - Sync framework.

            - Asynchronous

                - [fastapi](https://example.com/fastapi) - Async framework.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        sync = (
            site / "categories" / "web-frameworks"
            / "synchronous" / "index.html"
        ).read_text(encoding="utf-8")
        async_ = (
            site / "categories" / "web-frameworks"
            / "asynchronous" / "index.html"
        ).read_text(encoding="utf-8")

        assert "django" in sync
        assert "fastapi" not in sync
        assert "fastapi" in async_
        assert "django" not in async_

    def test_subcategory_page_shows_breadcrumb(self, tmp_path):
        """Test that subcategory page shows breadcrumb."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Web**

            ## Web Frameworks

            - Synchronous

                - [django](https://example.com/django) - Sync.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        sync = (
            site / "categories" / "web-frameworks"
            / "synchronous" / "index.html"
        ).read_text(encoding="utf-8")
        assert 'href="/categories/web-frameworks/"' in sync
        assert "Web Frameworks" in sync
        assert "<h1>Synchronous</h1>" in sync
        assert "category-breadcrumb" in sync

        parser = HeadMetadataParser()
        parser.feed(sync)
        assert parser.title.strip() == (
            "Synchronous for Web Frameworks - Awesome Python"
        )
        expected_desc = (
            "Explore 1 curated Python projects in "
            "Synchronous for Web Frameworks. "
            "Part of the Awesome Python catalog."
        )
        assert parser.meta_by_name["description"] == expected_desc

        marker = '<script type="application/ld+json">'
        start = sync.index(marker) + len(marker)
        end = sync.index("</script>", start)
        graph = {
            node["@type"]: node
            for node in json.loads(sync[start:end])["@graph"]
        }
        assert graph["CollectionPage"]["name"] == (
            "Synchronous for Web Frameworks"
        )
        expected_breadcrumbs = [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Awesome Python",
                "item": "https://awesome-python.com/",
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Web Frameworks",
                "item": "https://awesome-python.com/categories/web-frameworks/",
            },
            {
                "@type": "ListItem",
                "position": 3,
                "name": "Synchronous",
                "item": "https://awesome-python.com/categories/web-frameworks/synchronous/",
            },
        ]
        assert graph["BreadcrumbList"]["itemListElement"] == expected_breadcrumbs

        parent = (
            site / "categories" / "web-frameworks" / "index.html"
        ).read_text(encoding="utf-8")
        assert "category-breadcrumb" not in parent

    def test_sponsorship_page_contains_json_ld(self, tmp_path):
        """Test that sponsorship page contains JSON-LD."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Tools**

            ## Widgets

            - [w1](https://example.com/w1) - A widget.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        html = (
            site / "sponsorship" / "index.html"
        ).read_text(encoding="utf-8")
        parser = HeadMetadataParser()
        parser.feed(html)

        assert parser.title.strip() == "Sponsor Awesome Python"
        expected_desc = (
            "Sponsorship for awesome-python: tiers, "
            "audience, and how to get your product in "
            "front of professional Python developers "
            "evaluating tools for production use."
        )
        assert parser.meta_by_name["description"] == expected_desc
        assert (
            parser.links_by_rel["canonical"]
            == "https://awesome-python.com/sponsorship/"
        )
        assert (
            '<a href="/sponsorship/" class="hero-topbar-link">'
            "Sponsorship</a>" in html
        )

        marker = '<script type="application/ld+json">'
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        graph = {
            node["@type"]: node
            for node in json.loads(html[start:end])["@graph"]
        }

        assert set(graph) == {"WebSite", "WebPage", "BreadcrumbList"}
        assert (
            graph["WebPage"]["@id"]
            == "https://awesome-python.com/sponsorship/"
        )
        assert (
            graph["WebPage"]["url"]
            == "https://awesome-python.com/sponsorship/"
        )
        expected_breadcrumbs = [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Awesome Python",
                "item": "https://awesome-python.com/",
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Sponsorship",
                "item": "https://awesome-python.com/sponsorship/",
            },
        ]
        assert graph["BreadcrumbList"]["itemListElement"] == expected_breadcrumbs

    def test_index_embeds_filter_urls_json(self, tmp_path):
        """Test that index page embeds filter URLs JSON."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **AI & ML**

            ## Deep Learning

            - [dl1](https://example.com/dl1) - DL.

            ## Machine Learning

            - Classical

                - [ml1](https://example.com/ml1) - ML.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        index_html = (
            site / "index.html"
        ).read_text(encoding="utf-8")

        marker = '<script type="application/json" id="filter-urls">'
        assert marker in index_html
        start = index_html.index(marker) + len(marker)
        end = index_html.index("</script>", start)
        data = json.loads(index_html[start:end])

        assert data["Deep Learning"] == "/categories/deep-learning/"
        assert data["Machine Learning"] == "/categories/machine-learning/"
        assert data["AI & ML"] == "/categories/ai-ml/"
        assert (
            data["Machine Learning > Classical"]
            == "/categories/machine-learning/classical/"
        )

    def test_filter_urls_json_escapes_closing_script_tag(self, tmp_path):
        """Test that filter URLs JSON escapes closing script tags."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            ## Sneaky </script><script>x=1</script>

            - [a](https://example.com) - A.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        index_html = (
            site / "index.html"
        ).read_text(encoding="utf-8")

        marker = '<script type="application/json" id="filter-urls">'
        start = index_html.index(marker) + len(marker)
        end = index_html.index("</script>", start)
        block = index_html[start:end]
        assert "</script>" not in block
        data = json.loads(block)
        assert any("Sneaky" in key for key in data)

    def test_build_creates_group_pages(self, tmp_path):
        """Test that build creates group pages."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **AI & ML**

            ## Deep Learning

            - [dl1](https://example.com/dl1) - DL.

            ## Machine Learning

            - [ml1](https://example.com/ml1) - ML.

            **Web Development**

            ## Web Frameworks

            - [wf1](https://example.com/wf1) - WF.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        ai_ml = (
            site / "categories" / "ai-ml" / "index.html"
        ).read_text(encoding="utf-8")
        web_dev = (
            site / "categories" / "web-development" / "index.html"
        ).read_text(encoding="utf-8")

        assert "dl1" in ai_ml
        assert "ml1" in ai_ml
        assert "wf1" not in ai_ml
        assert "wf1" in web_dev
        assert "dl1" not in web_dev

    def test_tag_buttons_have_data_url(self, tmp_path):
        """Test that tag buttons have correct data URLs."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **AI & ML**

            ## Deep Learning

            - Vision

                - [v1](https://example.com/v1) - Vision lib.

            # Contributing

            Done.
        """)
        self._copy_real_templates(tmp_path)
        (tmp_path / "README.md").write_text(readme, encoding="utf-8")
        build(tmp_path)

        site = tmp_path / "website" / "output"
        index_html = (
            site / "index.html"
        ).read_text(encoding="utf-8")

        assert 'data-value="Deep Learning"' in index_html
        assert 'data-url="/categories/deep-learning/"' in index_html
        assert (
            'data-value="AI &amp; ML"' in index_html
            or 'data-value="AI & ML"' in index_html
        )
        assert 'data-url="/categories/ai-ml/"' in index_html
        assert (
            'data-url="/categories/deep-learning/vision/"'
            in index_html
        )


# ---------------------------------------------------------------------------
# extract_github_repo
# ---------------------------------------------------------------------------


class TestExtractGithubRepo:
    """Tests for the extract_github_repo function."""

    def test_github_url(self):
        """Test extracting repo from GitHub URL."""
        assert extract_github_repo(
            "https://github.com/psf/requests"
        ) == "psf/requests"

    def test_non_github_url(self):
        """Test extracting repo from non-GitHub URL."""
        assert extract_github_repo(
            "https://foss.heptapod.net/pypy/pypy"
        ) is None

    def test_github_io_url(self):
        """Test extracting repo from GitHub Pages URL."""
        assert extract_github_repo(
            "https://user.github.io/proj"
        ) is None

    def test_trailing_slash(self):
        """Test extracting repo with trailing slash."""
        assert extract_github_repo(
            "https://github.com/org/repo/"
        ) == "org/repo"

    def test_deep_path(self):
        """Test extracting repo from deep path."""
        assert extract_github_repo(
            "https://github.com/org/repo/tree/main"
        ) is None

    def test_dot_git_suffix(self):
        """Test extracting repo with .git suffix."""
        assert extract_github_repo(
            "https://github.com/org/repo.git"
        ) == "org/repo"

    def test_org_only(self):
        """Test extracting repo when only org is provided."""
        assert extract_github_repo(
            "https://github.com/org"
        ) is None


# ---------------------------------------------------------------------------
# load_stars
# ---------------------------------------------------------------------------


class TestLoadStars:
    """Tests for the load_stars function."""

    def test_returns_empty_when_missing(self, tmp_path):
        """Test loading stars from missing file."""
        result = load_stars(tmp_path / "nonexistent.json")
        assert result == {}

    def test_loads_valid_json(self, tmp_path):
        """Test loading stars from valid JSON."""
        data = {
            "psf/requests": {
                "stars": 52467,
                "owner": "psf",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            }
        }
        f = tmp_path / "stars.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        result = load_stars(f)
        assert result["psf/requests"]["stars"] == 52467

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        """Test loading stars from corrupt JSON."""
        f = tmp_path / "stars.json"
        f.write_text("not json", encoding="utf-8")
        result = load_stars(f)
        assert result == {}


# ---------------------------------------------------------------------------
# sort_entries
# ---------------------------------------------------------------------------


class TestLoadPypiBadges:
    """Tests for the load_pypi_badges function."""

    def test_only_entries_with_a_badge_are_returned(self):
        """Test that only entries with badges are returned."""
        badges = load_pypi_badges()
        assert badges["azure-sdk-for-python"] == "Multiple on PyPI"
        assert badges["google-cloud-python"] == "Multiple on PyPI"
        # Entries whose missing count needs no explaining stay off the map
        assert "cpython" not in badges
        assert "tomllib" not in badges


class TemplateEntryData:
    """Data holder for template entry creation."""

    def __init__(
        self,
        name: str,
        stars: int | None,
        source_type: str | None = None,
    ):
        self.name = name
        self.stars = stars
        self.source_type = source_type


def _template_entry(
    name: str, stars: int | None, source_type: str | None = None
) -> TemplateEntry:
    """Create a TemplateEntry for testing."""
    return TemplateEntry(
        name=name,
        url="",
        description="",
        categories=[],
        groups=[],
        subcategories=[],
        stars=stars,
        downloads=None,
        owner=None,
        last_commit_at=None,
        source_type=source_type,
        bundled=False,
        badge=None,
        also_see=[],
    )


class TestSortEntries:
    """Tests for the sort_entries function."""

    def test_sorts_by_stars_descending(self):
        """Test sorting entries by stars descending."""
        entries = [
            _template_entry("a", 100),
            _template_entry("b", 500),
            _template_entry("c", 200),
        ]
        result = sort_entries(entries)
        assert [e["name"] for e in result] == ["b", "c", "a"]

    def test_equal_stars_sorted_alphabetically(self):
        """Test sorting entries with equal stars alphabetically."""
        entries = [
            _template_entry("beta", 100),
            _template_entry("alpha", 100),
        ]
        result = sort_entries(entries)
        assert [e["name"] for e in result] == ["alpha", "beta"]

    def test_no_stars_go_to_bottom(self):
        """Test that entries with no stars go to the bottom."""
        entries = [
            _template_entry("no-stars", None),
            _template_entry("has-stars", 50),
        ]
        result = sort_entries(entries)
        assert [e["name"] for e in result] == ["has-stars", "no-stars"]

    def test_no_stars_sorted_alphabetically(self):
        """Test sorting entries with no stars alphabetically."""
        entries = [
            _template_entry("zebra", None),
            _template_entry("apple", None),
        ]
        result = sort_entries(entries)
        assert [e["name"] for e in result] == ["apple", "zebra"]

    def test_builtin_between_starred_and_unstarred(self):
        """Test that builtin entries are placed between starred and unstarred."""
        entries = [
            _template_entry("builtin", None, "Stdlib"),
            _template_entry("starred", 100),
            _template_entry("unstarred", None),
        ]
        result = sort_entries(entries)
        assert [e["name"] for e in result] == [
            "starred",
            "builtin",
            "unstarred",
        ]


# ---------------------------------------------------------------------------
# detect_source_type
# ---------------------------------------------------------------------------


class TestDetectSourceType:
    """Tests for the detect_source_type function."""

    def test_github_repo_returns_none(self):
        """Test that GitHub repo URLs return None."""
        assert detect_source_type(
            "https://github.com/psf/requests"
        ) is None

    def test_stdlib_url(self):
        """Test that stdlib URLs return 'Stdlib'."""
        assert detect_source_type(
            "https://docs.python.org/3/library/asyncio.html"
        ) == "Stdlib"

    def test_gitlab_url(self):
        """Test that GitLab URLs return 'GitLab'."""
        assert detect_source_type(
            "https://gitlab.com/org/repo"
        ) == "GitLab"

    def test_bitbucket_url(self):
        """Test that Bitbucket URLs return 'Bitbucket'."""
        assert detect_source_type(
            "https://bitbucket.org/org/repo"
        ) == "Bitbucket"

    def test_non_github_external(self):
        """Test that non-GitHub external URLs return 'External'."""
        assert detect_source_type(
            "https://example.com/tool"
        ) == "External"

    def test_github_non_repo_returns_none(self):
        """Test that non-repo GitHub URLs return None."""
        assert detect_source_type(
            "https://github.com/org/repo/wiki"
        ) is None


# ---------------------------------------------------------------------------
# extract_entries
# ---------------------------------------------------------------------------


class TestExtractEntries:
    """Tests for the extract_entries function."""

    def test_basic_extraction(self):
        """Test basic entry extraction."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Tools**

            ## Widgets

            - [widget](https://example.com) - A widget.

            # Contributing

            Done.
        """)
        groups = parse_readme(readme)
        categories = [c for g in groups for c in g["categories"]]
        entries = extract_entries(categories, groups)
        assert len(entries) == 1
        assert entries[0]["name"] == "widget"
        assert entries[0]["categories"] == ["Widgets"]
        assert entries[0]["groups"] == ["Tools"]

    def test_duplicate_entry_merged(self):
        """Test that duplicate entries are merged."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Tools**

            ## Alpha

            - [shared](https://example.com/shared) - Shared lib.

            ## Beta

            - [shared](https://example.com/shared) - Shared lib.

            # Contributing

            Done.
        """)
        groups = parse_readme(readme)
        categories = [c for g in groups for c in g["categories"]]
        entries = extract_entries(categories, groups)
        shared = [e for e in entries if e["name"] == "shared"]
        assert len(shared) == 1
        assert sorted(shared[0]["categories"]) == ["Alpha", "Beta"]

    def test_source_type_detected(self):
        """Test that source type is detected correctly."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            ## Stdlib

            - [asyncio](https://docs.python.org/3/library/asyncio.html) - Async I/O.

            # Contributing

            Done.
        """)
        groups = parse_readme(readme)
        categories = [c for g in groups for c in g["categories"]]
        entries = extract_entries(categories, groups)
        assert entries[0]["source_type"] == "Stdlib"

    def test_subcategory_includes_slug_and_url(self):
        """Test that subcategory includes slug and URL."""
        readme = textwrap.dedent("""\
            # T

            ## Projects

            **Tools**

            ## Web Frameworks

            - Synchronous

                - [django](https://example.com/django) - A framework.

            # Contributing

            Done.
        """)
        groups = parse_readme(readme)
        categories = [c for g in groups for c in g["categories"]]
        entries = extract_entries(categories, groups)
        expected_subcat = {
            "name": "Synchronous",
            "value": "Web Frameworks > Synchronous",
            "slug": "synchronous",
            "url": "/categories/web-frameworks/synchronous/",
        }
        assert entries[0]["subcategories"] == [expected_subcat]


# ---------------------------------------------------------------------------
# annotate_entries_with_stats
# ---------------------------------------------------------------------------


class TestAnnotateEntriesWithStats:
    """Tests for the annotate_entries_with_stats function."""

    def test_appends_star_count_to_bullet(self):
        """Test appending star count to bullet."""
        markdown = "- [foo](https://github.com/owner/foo) - A foo.\n"
        stars = {"owner/foo": {"stars": 123, "owner": "owner"}}
        result = annotate_entries_with_stats(markdown, stars, {})
        assert result == (
            "- [foo](https://github.com/owner/foo) - A foo. "
            "(GitHub stars: 123)\n"
        )

    def test_appends_downloads_by_display_name(self):
        """Test appending downloads by display name."""
        markdown = "- [Foo.py](https://example.com) - A foo.\n"
        result = annotate_entries_with_stats(markdown, {}, {"foo-py": 777})
        assert result == (
            "- [Foo.py](https://example.com) - A foo. "
            "(PyPI downloads/month: 777)\n"
        )

    def test_appends_downloads_and_stars_together(self):
        """Test appending both downloads and stars."""
        markdown = "- [foo](https://github.com/owner/foo) - A foo.\n"
        stars = {"owner/foo": {"stars": 123, "owner": "owner"}}
        result = annotate_entries_with_stats(
            markdown, stars, {"foo": 777}
        )
        assert result == (
            "- [foo](https://github.com/owner/foo) - A foo. "
            "(PyPI downloads/month: 777, GitHub stars: 123)\n"
        )

    def test_uses_first_github_link(self):
        """Test that the first GitHub link is used for stars."""
        markdown = (
            "- [foo](https://github.com/owner/foo) - A foo. "
            "Also [bar](https://github.com/owner/bar).\n"
        )
        stars = {
            "owner/foo": {"stars": 10, "owner": "owner"},
            "owner/bar": {"stars": 99, "owner": "owner"},
        }
        result = annotate_entries_with_stats(markdown, stars, {})
        assert result == (
            "- [foo](https://github.com/owner/foo) - A foo. "
            "Also [bar](https://github.com/owner/bar). "
            "(GitHub stars: 10)\n"
        )

    def test_skips_entries_without_data(self):
        """Test skipping entries without data."""
        markdown = "- [foo](https://github.com/owner/foo) - A foo.\n"
        result = annotate_entries_with_stats(markdown, {}, {})
        assert result == markdown

    def test_skips_non_github_links_for_stars(self):
        """Test skipping non-GitHub links for stars."""
        markdown = "- [foo](https://example.com) - A foo.\n"
        stars = {"owner/foo": {"stars": 1, "owner": "owner"}}
        result = annotate_entries_with_stats(markdown, stars, {})
        assert result == markdown

    def test_skips_non_bullet_lines(self):
        """Test skipping non-bullet lines."""
        markdown = (
            "See [foo](https://github.com/owner/foo) for details.\n"
        )
        stars = {"owner/foo": {"stars": 1, "owner": "owner"}}
        result = annotate_entries_with_stats(markdown, stars, {"foo": 5})
        assert result == markdown

    def test_handles_indented_bullets(self):
        """Test handling indented bullets."""
        markdown = "    - [foo](https://github.com/owner/foo)\n"
        stars = {"owner/foo": {"stars": 7, "owner": "owner"}}
        result = annotate_entries_with_stats(markdown, stars, {})
        assert result == (
            "    - [foo](https://github.com/owner/foo) "
            "(GitHub stars: 7)\n"
        )

    def test_preserves_lines_without_trailing_newline(self):
        """Test preserving lines without trailing newline."""
        markdown = "- [foo](https://github.com/owner/foo) - A foo."
        stars = {"owner/foo": {"stars": 5, "owner": "owner"}}
        result = annotate_entries_with_stats(markdown, stars, {})
        assert result == (
            "- [foo](https://github.com/owner/foo) - A foo. "
            "(GitHub stars: 5)"
        )


class TestLoadDownloads:
    """Tests for the load_downloads function."""

    def test_parses_tsv_and_skips_not_found(self, tmp_path):
        """Test parsing TSV and skipping NOT_FOUND entries."""
        tsv = tmp_path / "pypi_downloads.tsv"
        tsv.write_text(
            "name\tpackage\tdownloads\tfetched_at\n"
            "aiohttp\taiohttp\t649105404\t2026-08-16\n"
            "pytorch\ttorch\t50000000\t2026-08-16\n"
            "dead-pkg\t-\tNOT_FOUND\t2026-08-16\n",
            encoding="utf-8",
        )
        result = load_downloads(tsv)
        assert result == {
            "aiohttp": 649105404,
            "pytorch": 50000000,
        }

    def test_missing_file_returns_empty(self, tmp_path):
        """Test returning empty dict for missing file."""
        assert load_downloads(tmp_path / "nope.tsv") == {}
