"""The edge serves HTML, so it must send the headers an HTML surface needs.

Asserted against the template, where each directive's reason sits beside it.
Nothing here sees a real response header; that check belongs in the deploy
probe (123zgec1bvn). What the app itself serves is test_app_surface.py.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EDGE_TEMPLATE = REPO_ROOT / "deploy" / "nginx" / "edge.conf.template"


def _strip_comments(text: str) -> str:
    """Drop `#` to end of line, but not inside a quoted value."""
    out, quote = [], ""
    for line in text.splitlines():
        kept = []
        for ch in line:
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            kept.append(ch)
        out.append("".join(kept))
    return "\n".join(out)


def _statements(text: str) -> list[tuple[int, str, bool]]:
    """Every nginx statement as (depth, text, opens_a_block).

    Character-wise rather than line-wise, so a one-line `location / { ... }`, a
    tab after a directive name, and a directive wrapped across lines all parse
    the same as the tidy form.
    """
    out: list[tuple[int, str, bool]] = []
    depth, buf, quote, variable = 0, "", "", 0
    for ch in _strip_comments(text):
        if quote:
            # A quoted value may contain ; and braces: the CSP contains both.
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == "{" and buf.endswith("$"):
            # ${TFS_EDGE_HOST} is a variable, not a block.
            variable += 1
            buf += ch
        elif ch == "}" and variable:
            variable -= 1
            buf += ch
        elif ch == "{":
            out.append((depth, " ".join(buf.split()), True))
            depth += 1
            buf = ""
        elif ch == "}":
            if buf.strip():
                out.append((depth, " ".join(buf.split()), False))
            depth -= 1
            buf = ""
        elif ch == ";":
            out.append((depth, " ".join(buf.split()), False))
            buf = ""
        else:
            buf += ch
    # Loud rather than silent: an unterminated quote or brace otherwise swallows
    # every statement after it, and the assertions below would pass on a file
    # the parser stopped reading.
    assert not quote, "unterminated quote in the template"
    assert depth == 0, f"unbalanced braces in the template (depth {depth})"
    return out


@pytest.fixture(scope="module")
def public_blocks() -> list[list[tuple[int, str]]]:
    """Each server block reachable from the internet, as (depth relative to the
    block, text).

    Per block, not flattened: the template anticipates a second 8443 block, and
    every one of them has to carry the headers itself. A flattened view would
    let a headerless block pass on its neighbour's declaration, and would read
    two correct blocks as a duplicate policy. Scoped to 8443 because the
    template also carries a container-local health listener on 127.0.0.1:8080,
    where a header satisfies a file-wide search while reaching nobody.
    """
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] | None = None
    base = 0
    for depth, text, opens in _statements(EDGE_TEMPLATE.read_text()):
        if current is not None and depth < base:
            current = None
        if opens and text.startswith("server") and current is None:
            current, base = [], depth + 1
            blocks.append(current)
            continue
        if current is not None:
            current.append((depth - base, text))

    public = [b for b in blocks if any(re.match(r"listen\b.*\b8443\b", t) for _, t in b)]
    assert public, "no server block listens on 8443"

    # An include hides its contents from this scan. In a public block that is an
    # add_header we cannot see; in one with no `listen` it may be the listener
    # itself. Only a block that declares a non-8443 listener may include freely.
    unclassified = [b for b in blocks if not any(re.match(r"listen\b", t) for _, t in b)]
    hidden = [t for b in public + unclassified for _, t in b if re.match(r"include\b", t)]
    assert not hidden, f"an include puts a block beyond this scan: {hidden}"
    return public


def headers(block: list[tuple[int, str]], name: str) -> list[str]:
    """Every declaration of `name` at a block's own level."""
    pattern = re.compile(
        rf"""add_header\s+{re.escape(name)}\s+
            (?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>\S+))
            (?:\s+always)?$""",
        re.X,
    )
    found = []
    for depth, text in block:
        match = pattern.match(text)
        if match and depth == 0:
            found.append(match.group("dq") or match.group("sq") or match.group("bare"))
    return found


def header(block: list[tuple[int, str]], name: str) -> str:
    """The single declared value. nginx sends duplicates and the browser
    intersects them, so a second declaration is a finding, not a tie to break."""
    found = headers(block, name)
    assert found, f"the edge sends no {name}"
    assert len(found) == 1, f"{name} is declared {len(found)} times: {found}"
    return found[0]


@pytest.fixture(scope="module")
def csp(public_blocks) -> str:
    policies = {header(b, "Content-Security-Policy") for b in public_blocks}
    assert len(policies) == 1, f"the 8443 blocks disagree on the policy: {policies}"
    return policies.pop()


def directive(csp: str, name: str) -> str:
    match = re.search(rf"(?:^|;)\s*{re.escape(name)} ([^;]+)", csp)
    assert match, f"no {name} directive"
    return match.group(1).strip()


def test_edge_sends_a_content_security_policy(csp: str):
    assert directive(csp, "default-src") == "'self'"
    assert directive(csp, "script-src") == "'self'"
    assert directive(csp, "object-src") == "'none'"


def test_edge_refuses_to_be_framed(csp: str, public_blocks):
    assert directive(csp, "frame-ancestors") == "'self'"
    for block in public_blocks:
        assert header(block, "X-Frame-Options") == "SAMEORIGIN"


def test_navigation_directives_are_set_because_they_do_not_inherit(csp: str):
    """default-src covers neither, so omitting either leaves it unrestricted
    while the policy reads as locked down."""
    assert directive(csp, "base-uri") == "'self'"
    assert directive(csp, "form-action") == "'self'"


def test_csp_keeps_inline_styles(csp: str):
    """The markers depend on it; the template says why."""
    assert "'unsafe-inline'" in directive(csp, "style-src")


def test_csp_carries_every_image_source_the_map_needs(csp: str):
    """Exactly these three, for the reasons in the template. `data:` is not
    exercised today and is pinned so that stays a deliberate choice."""
    img_src = directive(csp, "img-src")
    assert img_src.split() == ["'self'", "data:", "https://*.basemaps.cartocdn.com"]


def test_permissions_policy_still_allows_the_location_button(public_blocks):
    """geolocation=() would refuse SearchForm's "Use My Location"."""
    for block in public_blocks:
        assert "geolocation=(self)" in header(block, "Permissions-Policy")


def test_every_header_is_sent_on_error_responses_too(public_blocks):
    """See the template."""
    for block in public_blocks:
        declared = [t for d, t in block if d == 0 and re.match(r"add_header\b", t)]
        assert declared, "an 8443 block declares no headers at all"
        for text in declared:
            assert re.search(r"\balways$", text), f"not sent on errors: {text}"


def test_no_nested_block_declares_its_own_add_header(public_blocks):
    """nginx's header filter replaces rather than merges, so one add_header
    inside a location discards every inherited one. That is how retina-server's
    SPA documents came to be served with no CSP at all (its snippets/spa.conf)."""
    offenders = [t for b in public_blocks for d, t in b if d > 0 and re.match(r"add_header\b", t)]
    assert not offenders, f"these discard the inherited security headers: {offenders}"


def _location(block: list[tuple[int, str]], path: str) -> tuple[str, list[str]] | None:
    """(modifier, statements) for the location matching `path`, or None if there
    is no such block. The modifier is "" for a bare prefix match."""
    for i, (depth, text) in enumerate(block):
        match = re.fullmatch(rf"location (?:(=|\^~|~\*?) )?{re.escape(path)}", text)
        if depth == 0 and match:
            body = []
            for inner_depth, inner in block[i + 1 :]:
                if inner_depth == 0:
                    break
                body.append(inner)
            return match.group(1) or "", body
    return None


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_the_edge_refuses_the_cdn_backed_docs_pages(public_blocks, path):
    """Refused rather than merely routed; the template says why."""
    for block in public_blocks:
        found = _location(block, path)
        assert found is not None, f"{path} has no location of its own"
        _, body = found
        assert any(s.startswith("return 404") for s in body), f"{path} is not refused: {body}"


def test_the_docs_refusal_reaches_the_oauth2_redirect(public_blocks):
    """FastAPI registers /docs/oauth2-redirect whenever the docs are on, and an
    exact match would leave it proxied."""
    for block in public_blocks:
        modifier, _ = _location(block, "/docs")
        assert modifier != "=", "an exact match leaves /docs/oauth2-redirect proxied"
