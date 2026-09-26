"""Renders the app's privacy policy into edge/src/privacy.html.

Google Play wants the privacy policy at a public URL, and the worker serves it
at /privacy. The text is not written here: it is read out of the app's own
`lib/features/legal/legal_documents.dart`, so the page a reviewer opens and the
screen a user opens in the app cannot drift apart. Edit the policy there, then
run this and deploy:

    python edge/build_privacy.py            # app repo assumed at ../zonevpn
    python edge/build_privacy.py PATH/TO/zonevpn
    bash edge/deploy.sh
"""

import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "zonevpn"
SOURCE = APP / "lib" / "features" / "legal" / "legal_documents.dart"
OUT = HERE / "src" / "privacy.html"


def dart_constants(src: str) -> dict:
    """The `static const String x = '...';` values, one line each."""
    return dict(re.findall(r"static const String (\w+) = '([^']*)';", src))


def dart_block(src: str, name: str) -> str:
    match = re.search(rf"static const String {name} = '''\n(.*?)''';", src, re.S)
    if not match:
        sys.exit(f"{name} not found in {SOURCE}")
    return match.group(1)


def dart_list(src: str, name: str) -> list:
    match = re.search(rf"{name} = \[(.*?)\];", src, re.S)
    if not match:
        sys.exit(f"{name} not found in {SOURCE}")
    # Adjacent Dart string literals concatenate, exactly as in the app.
    items = re.split(r"(?<='),\s*\n", match.group(1))
    return ["".join(re.findall(r"'([^']*)'", item)) for item in items if "'" in item]


def interpolate(text: str, consts: dict) -> str:
    return re.sub(r"\$(\w+)", lambda m: consts.get(m.group(1), m.group(0)), text)


def inline(text: str, email: str) -> str:
    out = html.escape(text, quote=False)
    out = out.replace(email, f'<a href="mailto:{email}">{email}</a>')
    return re.sub(r"\b(policies\.google\.com/privacy)\b",
                  r'<a href="https://\1">\1</a>', out)


def render_body(text: str, email: str) -> str:
    """Numbered headings, bullet lists and paragraphs, as the app lays them out."""
    parts, para, items = [], [], []

    def flush():
        if para:
            parts.append(f"<p>{inline(' '.join(para), email)}</p>")
            para.clear()
        if items:
            lis = "".join(f"<li>{inline(i, email)}</li>" for i in items)
            parts.append(f"<ul>{lis}</ul>")
            items.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif re.match(r"^\d+\. ", line):
            flush()
            parts.append(f"<h2>{inline(stripped, email)}</h2>")
        elif stripped.startswith("•"):
            if para:
                flush()
            items.append(stripped.lstrip("• ").strip())
        elif items and line.startswith("    "):
            items[-1] += " " + stripped
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return "\n".join(parts)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="How {app} handles information.">
<style>
:root {{ --bg:#fbfbfa; --fg:#1d1f22; --muted:#5d636b; --line:#e3e4e2; --accent:#0b6b5c; --card:#f2f3f1; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#111315; --fg:#e7e8e6; --muted:#9aa0a6; --line:#26292c; --accent:#5fd0bb; --card:#191c1e; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:44rem; margin:0 auto; padding:3rem 1rem 4rem; }}
h1 {{ font-size:1.75rem; line-height:1.25; margin:0 0 .25rem; letter-spacing:-.01em; }}
.meta {{ color:var(--muted); margin:0 0 2rem; font-size:.9rem; }}
.summary {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:1rem 1.25rem; margin:0 0 2.5rem; }}
.summary h2 {{ margin-top:0; font-size:.8rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
.summary ul {{ margin:0; }}
h2 {{ font-size:1.1rem; margin:2rem 0 .5rem; }}
p, li {{ overflow-wrap:anywhere; }}
ul {{ padding-left:1.25rem; }}
li {{ margin:.4rem 0; }}
a {{ color:var(--accent); }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<p class="meta">Effective {date}</p>
<section class="summary">
<h2>In short</h2>
<ul>{highlights}</ul>
</section>
{body}
</main>
</body>
</html>
"""


def main():
    src = SOURCE.read_text(encoding="utf-8")
    consts = dart_constants(src)
    email = consts["contactEmail"]
    text = interpolate(dart_block(src, "privacyPolicy"), consts)

    # The first two lines are the title and the date; the page sets those itself.
    lines = text.splitlines()
    title, body = lines[0].replace(" — ", " · "), "\n".join(lines[2:])
    highlights = "".join(f"<li>{inline(h, email)}</li>"
                         for h in dart_list(src, "privacyHighlights"))

    OUT.write_text(PAGE.format(
        title=html.escape(title), app=html.escape(consts["appName"]),
        date=html.escape(consts["effectiveDate"]), highlights=highlights,
        body=render_body(body, email)), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(HERE.parent)} ({OUT.stat().st_size} bytes) from {SOURCE}")


if __name__ == "__main__":
    main()
