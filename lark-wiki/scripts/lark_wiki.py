#!/usr/bin/env python3
"""Lark Wiki & Document API helper.

Uses lark_auth.py for authentication (tenant access token).
Config: ~/.lark-wiki/config.json  (app_id, app_secret)

Usage:
    python3 lark_wiki.py read <node_token>
    python3 lark_wiki.py list <node_token>
    python3 lark_wiki.py create <parent_node_token> <title> [--space-id ID]
    python3 lark_wiki.py blocks <document_id>
    python3 lark_wiki.py write <document_id> <blocks_json_file_or_string>
    python3 lark_wiki.py comment <document_id> <text>
    python3 lark_wiki.py tree <node_token> [--depth N]

Bitable (Base):
    python3 lark_wiki.py base-tables <app_token>
    python3 lark_wiki.py base-fields <app_token> <table_id>
    python3 lark_wiki.py base-records <app_token> <table_id> [--filter JSON]
    python3 lark_wiki.py base-add <app_token> <table_id> <fields_json>
    python3 lark_wiki.py base-update <app_token> <table_id> <record_id> <fields_json>
    python3 lark_wiki.py base-create-table <app_token> <table_json>

Contact:
    python3 lark_wiki.py contact-lookup <email1> [email2 ...]

Permissions:
    python3 lark_wiki.py perm-add <token> <member_id> [--file-type docx] [--perm edit]
    python3 lark_wiki.py perm-list <token> [--file-type docx]

Document Search:
    python3 lark_wiki.py doc-search <query> [--count 20] [--doc-types docx,sheet,bitable]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

from lark_auth import LarkAuth  # noqa: E402

_CONFIG_FILE = os.path.expanduser("~/.lark-wiki/config.json")
_auth = LarkAuth(_CONFIG_FILE)


def get_token():
    """Get a valid tenant access token."""
    try:
        return _auth.get_token()
    except RuntimeError:
        print(
            "Error: No Lark credentials found. "
            f"Create {_CONFIG_FILE} with app_id and app_secret.",
            file=sys.stderr,
        )
        sys.exit(1)


BASE = "https://open.larksuite.com/open-apis"


def api_get(path, token):
    """Make an authenticated GET request."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def api_post(path, token, body):
    """Make an authenticated POST request."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_read(args):
    """Read a wiki page's raw text content."""
    token = get_token()

    # Resolve node token to document token
    data = api_get(f"/wiki/v2/spaces/get_node?token={args.node_token}", token)
    node = data["data"]["node"]
    obj_token = node["obj_token"]
    obj_type = node.get("obj_type", "unknown")
    print(f"Title: {node.get('title', '(untitled)')}", file=sys.stderr)
    print(f"obj_token: {obj_token}", file=sys.stderr)
    print(f"obj_type: {obj_type}", file=sys.stderr)
    print(f"space_id: {node.get('space_id', 'unknown')}", file=sys.stderr)
    print("---", file=sys.stderr)

    # Check document type
    if obj_type == "bitable":
        print(
            "Error: This is a Bitable (spreadsheet), not a document.",
            file=sys.stderr,
        )
        print(
            "\nUse these commands instead:",
            file=sys.stderr,
        )
        print(
            f"  python3 lark_wiki.py base-tables {obj_token}  # List all tables",
            file=sys.stderr,
        )
        print(
            f"  python3 lark_wiki.py base-records {obj_token} <table_id>  # Query records",
            file=sys.stderr,
        )
        sys.exit(1)
    elif obj_type != "docx":
        print(
            f"Warning: Document type '{obj_type}' is not fully supported.",
            file=sys.stderr,
        )
        print("Attempting to read as docx document...", file=sys.stderr)

    # Read raw content (docx)
    try:
        content = api_get(f"/docx/v1/documents/{obj_token}/raw_content", token)
        print(content["data"]["content"])
    except urllib.error.HTTPError as e:
        if e.code == 404 or e.code == 1770002:
            print(
                f"Error: Could not read document as docx (HTTP {e.code}).",
                file=sys.stderr,
            )
            print(
                f"The document type '{obj_type}' may not be supported.",
                file=sys.stderr,
            )
        raise


def cmd_list(args):
    """List child nodes under a wiki node."""
    token = get_token()

    # Get node info to find space_id
    data = api_get(f"/wiki/v2/spaces/get_node?token={args.node_token}", token)
    node = data["data"]["node"]
    space_id = node["space_id"]
    print(f"Parent: {node.get('title', '(untitled)')} (space: {space_id})", file=sys.stderr)

    # List children with pagination
    all_nodes = []
    page_token = ""
    while True:
        url = f"/wiki/v2/spaces/{space_id}/nodes?parent_node_token={args.node_token}&page_size=50"
        if page_token:
            url += f"&page_token={page_token}"
        result = api_get(url, token)
        items = result.get("data", {}).get("items", [])
        all_nodes.extend(items)
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result.get("data", {}).get("page_token", "")

    # Output as JSON
    output = []
    for n in all_nodes:
        output.append({
            "node_token": n["node_token"],
            "title": n.get("title", ""),
            "obj_type": n.get("obj_type", ""),
            "has_child": n.get("has_child", False),
        })
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_tree(args):
    """Recursively list wiki nodes as a tree."""
    token = get_token()

    # Get node info to find space_id
    data = api_get(f"/wiki/v2/spaces/get_node?token={args.node_token}", token)
    node = data["data"]["node"]
    space_id = node["space_id"]

    max_depth = args.depth if args.depth else 3

    def list_children(parent_token, depth, prefix):
        if depth > max_depth:
            return
        all_nodes = []
        page_token = ""
        while True:
            url = f"/wiki/v2/spaces/{space_id}/nodes?parent_node_token={parent_token}&page_size=50"
            if page_token:
                url += f"&page_token={page_token}"
            result = api_get(url, token)
            items = result.get("data", {}).get("items", [])
            all_nodes.extend(items)
            if not result.get("data", {}).get("has_more"):
                break
            page_token = result.get("data", {}).get("page_token", "")

        for i, n in enumerate(all_nodes):
            is_last = i == len(all_nodes) - 1
            icon = "\U0001f4c1" if n.get("has_child") else "\U0001f4c4"
            connector = "\u2514\u2500" if is_last else "\u251c\u2500"
            print(f"{prefix}{connector} {icon} {n.get('title', '?')} ({n['node_token']})")
            if n.get("has_child"):
                child_prefix = prefix + ("   " if is_last else "\u2502  ")
                list_children(n["node_token"], depth + 1, child_prefix)

    print(f"\U0001f4c1 {node.get('title', '?')} ({args.node_token})")
    list_children(args.node_token, 1, "")


def cmd_create(args):
    """Create a new wiki node."""
    token = get_token()

    # Resolve space_id from parent node
    if args.space_id:
        space_id = args.space_id
    else:
        data = api_get(f"/wiki/v2/spaces/get_node?token={args.parent_node_token}", token)
        space_id = data["data"]["node"]["space_id"]

    body = {
        "obj_type": "docx",
        "parent_node_token": args.parent_node_token,
        "node_type": "origin",
        "title": args.title,
    }
    result = api_post(f"/wiki/v2/spaces/{space_id}/nodes", token, body)
    node = result["data"]["node"]
    print(json.dumps({
        "node_token": node["node_token"],
        "obj_token": node["obj_token"],
        "title": args.title,
        "space_id": space_id,
    }, indent=2))


def cmd_blocks(args):
    """Read document blocks (structured content)."""
    token = get_token()
    all_items = []
    page_token = ""
    while True:
        url = f"/docx/v1/documents/{args.document_id}/blocks?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        result = api_get(url, token)
        items = result.get("data", {}).get("items", [])
        all_items.extend(items)
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result.get("data", {}).get("page_token", "")
    print(json.dumps(all_items, indent=2, ensure_ascii=False))


def cmd_write(args):
    """Write blocks to a document."""
    token = get_token()

    # Parse blocks from file or string
    blocks_input = args.blocks
    if os.path.isfile(blocks_input):
        with open(blocks_input) as f:
            blocks = json.load(f)
    else:
        blocks = json.loads(blocks_input)

    # If blocks is a list, wrap in children
    if isinstance(blocks, list):
        body = {"children": blocks, "index": args.index}
    else:
        body = blocks

    result = api_post(
        f"/docx/v1/documents/{args.document_id}/blocks/{args.document_id}/children",
        token,
        body,
    )
    created = result.get("data", {}).get("children", [])
    print(json.dumps({
        "created_blocks": len(created),
        "block_ids": [c["block_id"] for c in created],
        "revision": result.get("data", {}).get("document_revision_id"),
    }, indent=2))


def cmd_comment(args):
    """Add a global comment to a document.

    Note: The Lark Open API only supports global (whole-document) comments
    for docx files. Inline comments anchored to specific text are a UI-only
    feature with no API support.
    """
    token = get_token()

    body = {
        "reply_list": {
            "replies": [{
                "content": {
                    "elements": [{
                        "type": "text_run",
                        "text_run": {"text": args.text},
                    }]
                }
            }]
        }
    }

    result = api_post(
        f"/drive/v1/files/{args.document_id}/comments?file_type=docx",
        token,
        body,
    )
    comment = result.get("data", {})
    print(json.dumps({
        "comment_id": comment.get("comment_id"),
    }, indent=2))


# ---------------------------------------------------------------------------
# Bitable (Base) Commands
# ---------------------------------------------------------------------------


def cmd_base_tables(args):
    """List tables in a bitable app."""
    token = get_token()
    all_tables = []
    page_token = ""
    while True:
        url = f"/bitable/v1/apps/{args.app_token}/tables?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        result = api_get(url, token)
        items = result.get("data", {}).get("items", [])
        all_tables.extend(items)
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result.get("data", {}).get("page_token", "")
    output = [{"table_id": t["table_id"], "name": t.get("name", "")} for t in all_tables]
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_base_fields(args):
    """List fields in a bitable table."""
    token = get_token()
    all_fields = []
    page_token = ""
    while True:
        url = f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/fields?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        result = api_get(url, token)
        items = result.get("data", {}).get("items", [])
        all_fields.extend(items)
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result.get("data", {}).get("page_token", "")
    output = []
    for f in all_fields:
        entry = {
            "field_id": f.get("field_id"),
            "field_name": f.get("field_name"),
            "type": f.get("type"),
            "ui_type": f.get("ui_type"),
        }
        if (f.get("property") or {}).get("options"):
            entry["options"] = [o.get("name") for o in f["property"]["options"]]
        output.append(entry)
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_base_records(args):
    """Search/list records in a bitable table."""
    token = get_token()
    body = {}
    if args.filter:
        body["filter"] = json.loads(args.filter)

    result = api_post(
        f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records/search",
        token,
        body,
    )
    items = result.get("data", {}).get("items", [])
    output = []
    for item in items:
        output.append({
            "record_id": item.get("record_id"),
            "fields": item.get("fields", {}),
        })
    print(json.dumps(output, indent=2, ensure_ascii=False))


def cmd_base_add(args):
    """Add a record to a bitable table."""
    token = get_token()
    fields = json.loads(args.fields)
    result = api_post(
        f"/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records",
        token,
        {"fields": fields},
    )
    record = result.get("data", {}).get("record", {})
    print(json.dumps({
        "record_id": record.get("record_id"),
        "fields": record.get("fields", {}),
    }, indent=2, ensure_ascii=False))


def cmd_base_update(args):
    """Update a record in a bitable table."""
    token = get_token()
    fields = json.loads(args.fields)
    body = json.dumps({"fields": fields}).encode()
    url = f"{BASE}/bitable/v1/apps/{args.app_token}/tables/{args.table_id}/records/{args.record_id}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    record = result.get("data", {}).get("record", {})
    print(json.dumps({
        "record_id": record.get("record_id"),
        "fields": record.get("fields", {}),
    }, indent=2, ensure_ascii=False))


def cmd_base_create_table(args):
    """Create a table in a bitable app."""
    token = get_token()
    table_def = json.loads(args.table_json)
    if "table" not in table_def:
        table_def = {"table": table_def}
    result = api_post(
        f"/bitable/v1/apps/{args.app_token}/tables",
        token,
        table_def,
    )
    print(json.dumps({
        "table_id": result.get("data", {}).get("table_id"),
    }, indent=2))


# ---------------------------------------------------------------------------
# Contact Commands
# ---------------------------------------------------------------------------


def cmd_contact_lookup(args):
    """Look up user IDs by email addresses."""
    token = get_token()
    body = {"emails": args.emails, "user_id_type": "open_id"}
    result = api_post("/contact/v3/users/batch_get_id", token, body)
    user_list = result.get("data", {}).get("user_list", [])
    output = []
    for u in user_list:
        output.append({
            "email": u.get("email", ""),
            "user_id": u.get("user_id", ""),
        })
    print(json.dumps(output, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Permission Commands
# ---------------------------------------------------------------------------


def api_request(method, path, token, body=None):
    """Make an authenticated request with any HTTP method."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def cmd_perm_add(args):
    """Add a collaborator to a document."""
    token = get_token()
    body = {
        "member_type": "openid",
        "member_id": args.member_id,
        "perm": args.perm,
    }
    result = api_post(
        f"/drive/v1/permissions/{args.token}/members?type={args.file_type}&need_notification=false",
        token,
        body,
    )
    member = result.get("data", {}).get("member", {})
    print(json.dumps({
        "member_id": member.get("member_id", ""),
        "member_type": member.get("member_type", ""),
        "perm": member.get("perm", ""),
    }, indent=2))


def cmd_perm_list(args):
    """List collaborators of a document."""
    token = get_token()
    result = api_get(
        f"/drive/v1/permissions/{args.token}/members?type={args.file_type}",
        token,
    )
    members = result.get("data", {}).get("items", [])
    output = []
    for m in members:
        output.append({
            "member_id": m.get("member_id", ""),
            "member_type": m.get("member_type", ""),
            "perm": m.get("perm", ""),
            "name": m.get("name", ""),
        })
    print(json.dumps(output, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Document Search Commands
# ---------------------------------------------------------------------------


def cmd_doc_search(args):
    """Search documents globally."""
    token = get_token()
    body = {
        "search_key": args.query,
        "count": args.count,
        "offset": 0,
    }
    if args.doc_types:
        type_map = {"doc": 1, "sheet": 2, "slide": 3, "bitable": 8, "docx": 22, "wiki": 30}
        types = []
        for t in args.doc_types.split(","):
            t = t.strip().lower()
            if t in type_map:
                types.append(type_map[t])
        if types:
            body["docs_types"] = types

    result = api_post("/suite/docs-api/search/object", token, body)
    docs = result.get("data", {}).get("docs_entities", [])
    output = []
    for d in docs:
        output.append({
            "title": d.get("title", ""),
            "docs_token": d.get("docs_token", ""),
            "docs_type": d.get("docs_type", ""),
            "url": d.get("url", ""),
            "owner_id": d.get("owner_id", ""),
        })
    has_more = result.get("data", {}).get("has_more", False)
    total = result.get("data", {}).get("total", 0)
    print(json.dumps({"total": total, "has_more": has_more, "results": output}, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Init Command
# ---------------------------------------------------------------------------


def cmd_init(_args):
    """Interactive setup: create or update ~/.lark-wiki/config.json."""
    config_path = _CONFIG_FILE
    config_dir = os.path.dirname(config_path)

    existing = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    print("Lark Wiki Credential Setup")
    print(f"Config: {config_path}")
    print()

    def prompt_field(display_name, existing_val, secret=False):
        if existing_val:
            display = "*" * 8 if secret else existing_val
            choice = input(f"{display_name} (current: {display}) — keep? [Y/n]: ").strip()
            if choice.lower() not in ("n", "no"):
                return existing_val
        val = input(f"{display_name}: ").strip()
        while not val:
            print(f"  {display_name} cannot be empty.")
            val = input(f"{display_name}: ").strip()
        return val

    app_id = prompt_field("App ID", existing.get("app_id", ""))
    app_secret = prompt_field("App Secret", existing.get("app_secret", ""), secret=True)

    config = {**existing, "app_id": app_id, "app_secret": app_secret}

    os.makedirs(config_dir, exist_ok=True)
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"\nCredentials saved to {config_path}")
    print("Run any lark-wiki command to verify the credentials work.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Lark Wiki & Document API helper")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Create or update ~/.lark-wiki/config.json interactively")

    # read
    p = sub.add_parser("read", help="Read wiki page raw text")
    p.add_argument("node_token", help="Wiki node token from URL")

    # list
    p = sub.add_parser("list", help="List child nodes")
    p.add_argument("node_token", help="Parent wiki node token")

    # tree
    p = sub.add_parser("tree", help="Recursive tree listing")
    p.add_argument("node_token", help="Root wiki node token")
    p.add_argument("--depth", type=int, default=3, help="Max depth (default: 3)")

    # create
    p = sub.add_parser("create", help="Create a new wiki node")
    p.add_argument("parent_node_token", help="Parent node token")
    p.add_argument("title", help="Title for the new node")
    p.add_argument("--space-id", help="Wiki space ID (auto-detected if omitted)")

    # blocks
    p = sub.add_parser("blocks", help="Read document blocks (structured)")
    p.add_argument("document_id", help="Document obj_token")

    # write
    p = sub.add_parser("write", help="Write blocks to a document")
    p.add_argument("document_id", help="Document obj_token")
    p.add_argument("blocks", help="JSON blocks (string or file path)")
    p.add_argument("--index", type=int, default=-1, help="Insert index (-1 = append)")

    # comment
    p = sub.add_parser("comment", help="Add a global comment to a document")
    p.add_argument("document_id", help="Document obj_token")
    p.add_argument("text", help="Comment text")

    # base-tables
    p = sub.add_parser("base-tables", help="List tables in a bitable app")
    p.add_argument("app_token", help="Bitable app token")

    # base-fields
    p = sub.add_parser("base-fields", help="List fields in a bitable table")
    p.add_argument("app_token", help="Bitable app token")
    p.add_argument("table_id", help="Table ID")

    # base-records
    p = sub.add_parser("base-records", help="Search/list records in a bitable table")
    p.add_argument("app_token", help="Bitable app token")
    p.add_argument("table_id", help="Table ID")
    p.add_argument("--filter", help="Filter JSON (Lark filter format)")

    # base-add
    p = sub.add_parser("base-add", help="Add a record to a bitable table")
    p.add_argument("app_token", help="Bitable app token")
    p.add_argument("table_id", help="Table ID")
    p.add_argument("fields", help="Fields JSON (e.g. '{\"Name\": \"value\"}')")

    # base-update
    p = sub.add_parser("base-update", help="Update a record in a bitable table")
    p.add_argument("app_token", help="Bitable app token")
    p.add_argument("table_id", help="Table ID")
    p.add_argument("record_id", help="Record ID to update")
    p.add_argument("fields", help="Fields JSON to update")

    # base-create-table
    p = sub.add_parser("base-create-table", help="Create a table in a bitable app")
    p.add_argument("app_token", help="Bitable app token")
    p.add_argument("table_json", help="Table definition JSON (name, fields, etc.)")

    # contact-lookup
    p = sub.add_parser("contact-lookup", help="Look up user IDs by email")
    p.add_argument("emails", nargs="+", help="Email addresses to look up")

    # perm-add
    p = sub.add_parser("perm-add", help="Add a collaborator to a document")
    p.add_argument("token", help="Document/file token")
    p.add_argument("member_id", help="Member open_id (from contact-lookup)")
    p.add_argument("--file-type", default="docx", help="File type: docx, sheet, bitable, etc. (default: docx)")
    p.add_argument("--perm", default="edit", help="Permission: view, edit, full_access (default: edit)")

    # perm-list
    p = sub.add_parser("perm-list", help="List collaborators of a document")
    p.add_argument("token", help="Document/file token")
    p.add_argument("--file-type", default="docx", help="File type: docx, sheet, bitable, etc. (default: docx)")

    # doc-search
    p = sub.add_parser("doc-search", help="Search documents globally")
    p.add_argument("query", help="Search query")
    p.add_argument("--count", type=int, default=20, help="Max results (default: 20)")
    p.add_argument("--doc-types", help="Comma-separated types: doc,docx,sheet,bitable,slide,wiki")

    args = parser.parse_args()

    try:
        {
            "init": cmd_init,
            "read": cmd_read,
            "list": cmd_list,
            "tree": cmd_tree,
            "create": cmd_create,
            "blocks": cmd_blocks,
            "write": cmd_write,
            "comment": cmd_comment,
            "base-tables": cmd_base_tables,
            "base-fields": cmd_base_fields,
            "base-records": cmd_base_records,
            "base-add": cmd_base_add,
            "base-update": cmd_base_update,
            "base-create-table": cmd_base_create_table,
            "contact-lookup": cmd_contact_lookup,
            "perm-add": cmd_perm_add,
            "perm-list": cmd_perm_list,
            "doc-search": cmd_doc_search,
        }[args.command](args)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            err_json = json.loads(err)
            print(json.dumps(err_json, indent=2, ensure_ascii=False), file=sys.stderr)
        except json.JSONDecodeError:
            print(f"HTTP {e.code}: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
