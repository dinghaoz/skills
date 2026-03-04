#!/usr/bin/env python3
"""Browser-based Lark wiki operations using Playwright.

Handles operations that the Lark Open API cannot do, such as adding
inline comments to specific text selections in documents.

Requires: pip install playwright && python3 -m playwright install chromium
Session data: ~/.playwright-lark-session (persistent browser context)
"""

import argparse
import json
import os
import re
import sys
import time

# Lark wiki base URL — set LARK_BASE env var to your organization's domain
# e.g. https://yourcompany.larksuite.com or https://yourcompany.feishu.cn
_env_base = os.environ.get("LARK_BASE", "").rstrip("/")
if not _env_base:
    print("Error: LARK_BASE environment variable is not set.", file=sys.stderr)
    print("Set it to your organization's Lark/Feishu domain, e.g.:", file=sys.stderr)
    print("  export LARK_BASE=https://yourcompany.larksuite.com", file=sys.stderr)
    sys.exit(1)
LARK_BASE = _env_base
SESSION_DIR = os.path.expanduser("~/.playwright-lark-session")


def ensure_playwright():
    """Check that playwright is installed."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print("Error: playwright is not installed.", file=sys.stderr)
        print("Install with: pip3 install --break-system-packages playwright && python3 -m playwright install chromium", file=sys.stderr)
        sys.exit(1)


def resolve_url(token_or_url):
    """Convert a node_token or full URL to a wiki URL."""
    if token_or_url.startswith("http"):
        return token_or_url
    return f"{LARK_BASE}/wiki/{token_or_url}"


def wait_for_doc_loaded(page, timeout=30):
    """Wait for the Lark document to fully load (past login)."""
    for _ in range(timeout):
        url = page.url
        if "accounts.larksuite.com" not in url and "login" not in url:
            page.wait_for_timeout(3000)  # Extra time for doc content
            return True
        page.wait_for_timeout(1000)
    return False


def find_toolbar_comment_button(page):
    """Find the AddCommentOutlined icon position in the floating toolbar."""
    icons = page.evaluate('''() => {
        const svgs = document.querySelectorAll('svg[data-icon="AddCommentOutlined"]');
        for (const svg of svgs) {
            const rect = svg.getBoundingClientRect();
            // Must be in the toolbar area (visible, reasonable size)
            if (rect.width > 0 && rect.height > 0 && rect.width < 30) {
                // Check if any ancestor is hidden
                let el = svg;
                let visible = true;
                while (el && el !== document.body) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') {
                        visible = false;
                        break;
                    }
                    el = el.parentElement;
                }
                if (visible) {
                    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                }
            }
        }
        return null;
    }''')
    return icons


def cmd_login(args):
    """Launch headed browser for manual Lark login."""
    sync_playwright = ensure_playwright()
    os.makedirs(SESSION_DIR, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{LARK_BASE}/wiki", timeout=30000)

        print("Browser opened. Please log in manually.")
        print("The session will be saved for future use.")
        print("Press Ctrl+C when done.")

        try:
            # Wait indefinitely until user closes or Ctrl+C
            while True:
                page.wait_for_timeout(5000)
                url = page.url
                if "accounts.larksuite.com" not in url and "login" not in url:
                    print(f"Logged in! Current URL: {url}")
        except KeyboardInterrupt:
            print("\nSession saved.")
        finally:
            context.close()


def _find_comment_input(page):
    """Find the comment input element in the comment panel.

    Searches for contenteditable divs with comment-related placeholders,
    or any contenteditable inside a comment panel on the right side.
    """
    return page.evaluate('''() => {
        // Strategy 1: placeholder attribute
        for (const ph of ["Add a comment", "Reply", "添加评论", "回复"]) {
            const els = document.querySelectorAll(`[placeholder="${ph}"]`);
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                // Must be in the right panel area (x > 900) and visible
                if (rect.width > 50 && rect.height > 10 && rect.x > 900) {
                    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                }
            }
        }

        // Strategy 2: contenteditable inside comment panel
        const panels = document.querySelectorAll('[class*="comment-panel"]');
        for (const panel of panels) {
            const rect = panel.getBoundingClientRect();
            if (rect.width === 0) continue;
            const editables = panel.querySelectorAll('[contenteditable="true"]');
            for (const el of editables) {
                const r = el.getBoundingClientRect();
                if (r.width > 50 && r.height > 10) {
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
            }
        }

        // Strategy 3: any contenteditable with comment-related class
        const candidates = document.querySelectorAll('[class*="comment"] [contenteditable="true"], [class*="card-panel"] [contenteditable="true"]');
        for (const el of candidates) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 50 && rect.height > 10 && rect.x > 900) {
                return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
            }
        }

        return null;
    }''')


def cmd_inline_comment(args):
    """Add an inline comment to specific text in a document."""
    sync_playwright = ensure_playwright()

    if not os.path.exists(SESSION_DIR):
        print("Error: No saved session. Run 'login' first to authenticate.", file=sys.stderr)
        sys.exit(1)

    url = resolve_url(args.document)
    search_text = args.search
    comment_text = args.comment
    headless = args.headless  # Default: headed (Lark toolbar doesn't render in headless)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # Navigate to document
            page.goto(url, timeout=30000)

            if not wait_for_doc_loaded(page):
                print(json.dumps({"ok": False, "error": "Login required. Run 'login' first."}))
                return

            page.wait_for_timeout(3000)  # Let doc fully render

            # Find and select the target text
            target = page.locator(f"text={search_text}").first
            bbox = target.bounding_box()
            if not bbox:
                print(json.dumps({"ok": False, "error": f"Text not found: {search_text}"}))
                return

            # Triple-click to select
            target.click(click_count=3)
            page.wait_for_timeout(1500)

            # Try keyboard shortcut first (most reliable), fall back to toolbar
            page.keyboard.press("Meta+Shift+m")
            page.wait_for_timeout(2000)

            # Check if comment panel opened
            input_el = _find_comment_input(page)

            if not input_el:
                # Fallback: re-select and try toolbar button
                target.click(click_count=3)
                page.wait_for_timeout(1500)
                comment_btn = find_toolbar_comment_button(page)
                if comment_btn:
                    page.mouse.click(comment_btn["x"], comment_btn["y"])
                    page.wait_for_timeout(2000)
                    input_el = _find_comment_input(page)

            if not input_el:
                print(json.dumps({"ok": False, "error": "Comment input not found"}))
                return

            # Click input, type comment, and submit
            page.mouse.click(input_el["x"], input_el["y"])
            page.wait_for_timeout(500)
            page.keyboard.type(comment_text, delay=15)
            page.wait_for_timeout(500)

            # Click Post button
            post_btn = page.locator('button:has-text("Post"):visible')
            if post_btn.count() == 0:
                print(json.dumps({"ok": False, "error": "Post button not found"}))
                return

            post_btn.first.click()
            page.wait_for_timeout(3000)

            # Verify success by checking for "Saved to cloud" or comment count
            print(json.dumps({
                "ok": True,
                "document": url,
                "search_text": search_text,
                "comment": comment_text,
            }))

        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
        finally:
            context.close()


def cmd_screenshot(args):
    """Take a screenshot of a Lark document."""
    sync_playwright = ensure_playwright()

    if not os.path.exists(SESSION_DIR):
        print("Error: No saved session. Run 'login' first.", file=sys.stderr)
        sys.exit(1)

    url = resolve_url(args.document)
    output = args.output or "/private/tmp/claude/lark-screenshot.png"
    os.makedirs(os.path.dirname(output), exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(url, timeout=30000)
            if not wait_for_doc_loaded(page):
                print(json.dumps({"ok": False, "error": "Login required"}))
                return

            page.wait_for_timeout(3000)
            page.screenshot(path=output, full_page=args.full_page)
            print(json.dumps({"ok": True, "path": output}))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
        finally:
            context.close()


def cmd_highlights(args):
    """Extract highlighted text from a document via the API (no browser needed).

    Uses the Lark Docx blocks API to find text with background_color set.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from lark_wiki import get_token, api_get

    # Resolve node_token to obj_token if needed
    doc_id = args.document
    if len(doc_id) < 30:
        # Might be a node_token, resolve it
        token = get_token()
        result = api_get(f"/wiki/v2/spaces/get_node?token={doc_id}", token)
        doc_id = result.get("data", {}).get("node", {}).get("obj_token", doc_id)

    token = get_token()

    # Fetch all blocks
    all_items = []
    page_token = ""
    while True:
        url = f"/docx/v1/documents/{doc_id}/blocks?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        result = api_get(url, token)
        items = result.get("data", {}).get("items", [])
        all_items.extend(items)
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result.get("data", {}).get("page_token", "")

    # Find highlighted text
    COLOR_NAMES = {
        1: "light_grey", 2: "light_purple", 3: "yellow",
        4: "light_green", 5: "pink", 6: "light_blue",
        7: "orange", 8: "light_red", 9: "dark_grey",
    }

    highlighted = []
    for block in all_items:
        for key in block:
            section = block[key]
            if isinstance(section, dict) and "elements" in section:
                for elem in section["elements"]:
                    tr = elem.get("text_run", {})
                    style = tr.get("text_element_style", {})
                    bg = style.get("background_color")
                    if bg is not None and bg > 0:
                        highlighted.append({
                            "block_id": block["block_id"],
                            "text": tr.get("content", ""),
                            "background_color": bg,
                            "color_name": COLOR_NAMES.get(bg, f"color_{bg}"),
                        })

    print(json.dumps({"ok": True, "count": len(highlighted), "highlights": highlighted}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Browser-based Lark wiki operations")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # login
    sub.add_parser("login", help="Launch browser for manual Lark login")

    # inline-comment
    ic = sub.add_parser("inline-comment", help="Add inline comment to text")
    ic.add_argument("document", help="Node token or full wiki URL")
    ic.add_argument("--search", required=True, help="Text to select for the comment")
    ic.add_argument("--comment", required=True, help="Comment text to add")
    ic.add_argument("--headless", action="store_true", help="Run in headless mode (less reliable, Lark toolbar may not render)")

    # screenshot
    ss = sub.add_parser("screenshot", help="Take screenshot of a document")
    ss.add_argument("document", help="Node token or full wiki URL")
    ss.add_argument("--output", "-o", help="Output path (default: /private/tmp/claude/lark-screenshot.png)")
    ss.add_argument("--full-page", action="store_true", help="Capture full page (not just viewport)")

    # highlights
    hl = sub.add_parser("highlights", help="Extract highlighted text via API")
    hl.add_argument("document", help="Node token or document obj_token")

    args = parser.parse_args()

    if args.command == "login":
        cmd_login(args)
    elif args.command == "inline-comment":
        cmd_inline_comment(args)
    elif args.command == "screenshot":
        cmd_screenshot(args)
    elif args.command == "highlights":
        cmd_highlights(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
