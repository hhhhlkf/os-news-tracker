import json

from app.sources.html_list_discovery import (
    HTML_LIST_DISCOVERY_PROMPT,
    discover_html_list_source,
)


ROCKY_LIKE_HTML = """
<html><body>
  <a href="/news/2026-06-18-self-2026-recap">
    <div class="rounded-xl border">
      <div><h3>Rocky Linux at SouthEast Linux Fest 2026</h3><p class="text-sm text-muted-foreground">June 18, 2026</p></div>
    </div>
  </a>
  <a href="/news/rocky-linux-10-2-ga-release">
    <div class="rounded-xl border">
      <div><h3>Rocky Linux 10.2 Available Now</h3><p class="text-sm text-muted-foreground">May 28, 2026</p></div>
    </div>
  </a>
  <a href="/about"><span>About</span></a>
</body></html>
"""


def test_discovers_rocky_like_news_list_selectors_without_llm():
    result = discover_html_list_source(
        "https://rockylinux.org/news",
        html=ROCKY_LIKE_HTML,
        llm=object(),
    )

    assert result.success is True
    assert result.used_llm is False
    assert result.link_selector == 'a[href^="/news/"]'
    assert result.title_selector == "h3"
    assert result.date_selector == "p.text-sm.text-muted-foreground"
    assert [item.title for item in result.sample_items] == [
        "Rocky Linux at SouthEast Linux Fest 2026",
        "Rocky Linux 10.2 Available Now",
    ]
    assert result.sample_items[0].date_text == "June 18, 2026"


def test_falls_back_to_llm_when_deterministic_selector_is_incomplete():
    class StubLlm:
        def __init__(self):
            self.prompt = ""

        def complete(self, prompt):
            self.prompt = prompt
            return json.dumps({
                "link_selector": "a.card-link",
                "title_selector": "h2",
                "date_selector": "time",
                "reason": "卡片链接、标题和日期结构重复出现",
            })

    html = """
    <html><body>
      <article><a class="card-link" href="/entry/2026-06-01-one-release"><h2>One</h2><time>2026-06-01</time></a></article>
      <article><a class="card-link" href="/entry/2026-06-02-two-release"><h2>Two</h2><time>2026-06-02</time></a></article>
    </body></html>
    """
    llm = StubLlm()

    result = discover_html_list_source("https://example.com/updates", html=html, llm=llm)

    assert result.success is True
    assert result.used_llm is True
    assert result.link_selector == "a.card-link"
    assert result.title_selector == "h2"
    assert result.date_selector == "time"
    assert "你是新闻列表页结构识别助手" in llm.prompt
    assert HTML_LIST_DISCOVERY_PROMPT.startswith("你是新闻列表页结构识别助手")
