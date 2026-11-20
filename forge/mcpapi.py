"""MCP endpoint: JSON-RPC 2.0 over HTTP, tools from a registry.

Each tool is ``(name, description, input_schema, handler)``. A handler
receives the forge and the arguments dict and returns a JSON-able
value; the endpoint wraps it as a text content block.
"""

from __future__ import annotations

import json

from . import model, objects as obj
from .shapes import issue_shape, pull_shape, repo_shape

PROTOCOL_VERSION = "2025-03-26"

TOOLS: dict[str, dict] = {}


def tool(name: str, description: str, schema: dict):
    """Register an MCP tool."""
    def wrap(fn):
        TOOLS[name] = {"name": name, "description": description,
                       "inputSchema": schema, "handler": fn}
        return fn
    return wrap


def _repo_arg(forge: model.ForgeState, args: dict) -> model.Repository:
    owner = args.get("owner", "")
    repo_name = args.get("repo", "")
    repo = forge.find_repo(owner, repo_name)
    if repo is None:
        raise ValueError(f"Repository not found: {owner}/{repo_name}")
    return repo


def _issue_arg(forge: model.ForgeState, args: dict) -> tuple:
    repo = _repo_arg(forge, args)
    number = int(args.get("issue_number", 0))
    issue = repo.issues.get(number)
    if issue is None:
        raise ValueError(f"Issue not found: #{number}")
    return repo, issue


def _pull_arg(forge: model.ForgeState, args: dict) -> tuple:
    repo = _repo_arg(forge, args)
    number = int(args.get("pull_number", 0))
    pr = repo.pulls.get(number)
    if pr is None:
        raise ValueError(f"Pull request not found: #{number}")
    return repo, pr


@tool("search_repositories",
      "Search for GitHub repositories.",
      {"type": "object",
       "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def _t_search_repos(forge, args):
    query = (args.get("query") or "").lower()
    items = []
    for repo in forge.repositories.values():
        hay = f"{repo.owner}/{repo.name} {repo.description}".lower()
        if not query or query in hay:
            items.append(repo_shape(forge, repo))
    return {"total_count": len(items), "items": items}


@tool("search_issues",
      "Search for GitHub issues and pull requests.",
      {"type": "object",
       "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def _t_search_issues(forge, args):
    query = (args.get("query") or "").lower()
    items = []
    for repo in forge.repositories.values():
        for issue in repo.issues.values():
            if not query or query in f"{issue.title} {issue.body}".lower():
                items.append(issue_shape(forge, repo, issue))
        for pr in repo.pulls.values():
            if not query or query in f"{pr.title} {pr.body}".lower():
                items.append(pull_shape(forge, repo, pr))
    return {"total_count": len(items), "items": items}


@tool("create_repository",
      "Create a new GitHub repository.",
      {"type": "object",
       "properties": {"name": {"type": "string"},
                      "description": {"type": "string"},
                      "private": {"type": "boolean"}},
       "required": ["name"]})
def _t_create_repo(forge, args):
    repo = forge.create_repo("adaiversen", args["name"],
                             description=args.get("description", ""),
                             private=bool(args.get("private", False)))
    return repo_shape(forge, repo)


@tool("get_repository",
      "Get a GitHub repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"}},
       "required": ["owner", "repo"]})
def _t_get_repo(forge, args):
    return repo_shape(forge, _repo_arg(forge, args))


@tool("list_repositories",
      "List repositories for a user.",
      {"type": "object",
       "properties": {"owner": {"type": "string"}}})
def _t_list_repos(forge, args):
    owner = args.get("owner", "")
    items = [repo_shape(forge, r)
             for r in forge.repositories.values()
             if not owner or r.owner == owner]
    return {"total_count": len(items), "items": items}


@tool("create_issue",
      "Create a new issue in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "title": {"type": "string"},
                      "body": {"type": "string"},
                      "labels": {"type": "array", "items": {"type": "string"}}},
       "required": ["owner", "repo", "title"]})
def _t_create_issue(forge, args):
    repo = _repo_arg(forge, args)
    issue = repo.open_issue(args["title"], args.get("body", ""), "adaiversen",
                            args.get("labels", []))
    issue.touch()
    return issue_shape(forge, repo, issue)


@tool("get_issue",
      "Get an issue from a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "issue_number": {"type": "integer"}},
       "required": ["owner", "repo", "issue_number"]})
def _t_get_issue(forge, args):
    repo, issue = _issue_arg(forge, args)
    return issue_shape(forge, repo, issue)


@tool("list_issues",
      "List issues in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "state": {"type": "string"}},
       "required": ["owner", "repo"]})
def _t_list_issues(forge, args):
    repo = _repo_arg(forge, args)
    state = args.get("state", "open")
    items = []
    for issue in repo.issues.values():
        if state != "all" and issue.state != state:
            continue
        items.append(issue_shape(forge, repo, issue))
    return {"total_count": len(items), "items": items}


@tool("update_issue",
      "Update an issue in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "issue_number": {"type": "integer"},
                      "title": {"type": "string"},
                      "body": {"type": "string"},
                      "state": {"type": "string"}},
       "required": ["owner", "repo", "issue_number"]})
def _t_update_issue(forge, args):
    repo, issue = _issue_arg(forge, args)
    if "title" in args:
        issue.title = args["title"]
    if "body" in args:
        issue.body = args["body"]
    if "state" in args:
        issue.state = args["state"]
        issue.closed_at = (obj.FIXED_DATE + "T00:00:00Z"
                           if args["state"] == "closed" else None)
    issue.touch()
    return issue_shape(forge, repo, issue)


@tool("add_issue_comment",
      "Add a comment to an issue.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "issue_number": {"type": "integer"},
                      "body": {"type": "string"}},
       "required": ["owner", "repo", "issue_number", "body"]})
def _t_add_comment(forge, args):
    repo, issue = _issue_arg(forge, args)
    import hashlib
    comment = {"id": forge.next_id(),
               "node_id": "IC_kwDO" + hashlib.sha1(
                   f"c:{forge._seq}".encode()).hexdigest()[:20],
               "body": args["body"], "author": "adaiversen",
               "created_at": obj.FIXED_DATE + "T00:00:00Z",
               "updated_at": obj.FIXED_DATE + "T00:00:00Z"}
    issue.comments.append(comment)
    issue.touch()
    return {"id": comment["id"], "body": comment["body"]}


@tool("create_pull_request",
      "Create a pull request in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "title": {"type": "string"},
                      "body": {"type": "string"},
                      "head": {"type": "string"},
                      "base": {"type": "string"},
                      "draft": {"type": "boolean"}},
       "required": ["owner", "repo", "title", "head", "base"]})
def _t_create_pull(forge, args):
    repo = _repo_arg(forge, args)
    pr = repo.open_pull(args["title"], args.get("body", ""),
                        args["head"], args["base"], "adaiversen",
                        draft=bool(args.get("draft", False)))
    return pull_shape(forge, repo, pr)


@tool("get_pull_request",
      "Get a pull request from a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "pull_number": {"type": "integer"}},
       "required": ["owner", "repo", "pull_number"]})
def _t_get_pull(forge, args):
    repo, pr = _pull_arg(forge, args)
    return pull_shape(forge, repo, pr)


@tool("list_pull_requests",
      "List pull requests in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "state": {"type": "string"}},
       "required": ["owner", "repo"]})
def _t_list_pulls(forge, args):
    repo = _repo_arg(forge, args)
    state = args.get("state", "open")
    items = []
    for pr in repo.pulls.values():
        if state != "all" and pr.state != state:
            continue
        items.append(pull_shape(forge, repo, pr))
    return {"total_count": len(items), "items": items}


@tool("merge_pull_request",
      "Merge a pull request.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "pull_number": {"type": "integer"},
                      "merge_method": {"type": "string"}},
       "required": ["owner", "repo", "pull_number"]})
def _t_merge_pull(forge, args):
    repo, pr = _pull_arg(forge, args)
    if pr.state != "open":
        raise ValueError("Pull request is not open")
    result = repo.squash_merge(pr.number)
    return result


@tool("get_file_contents",
      "Get the contents of a file in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "path": {"type": "string"},
                      "branch": {"type": "string"}},
       "required": ["owner", "repo", "path"]})
def _t_get_file(forge, args):
    import base64
    repo = _repo_arg(forge, args)
    branch = args.get("branch", repo.default_branch)
    tip = repo.refs.get("refs/heads/" + branch, "")
    flat: dict[str, str] = {}

    def walk(tree_oid: str, prefix: str) -> None:
        blob = repo.objects.get(tree_oid)
        if blob is None:
            return
        for mode, name, child in obj.decode_tree(
                obj.load_raw("tree", blob)):
            path = prefix + name
            if mode == "40000":
                walk(child, path + "/")
            else:
                flat[path] = child

    if tip and tip in repo.objects:
        parsed = obj.decode_commit(obj.load_raw("commit", repo.objects[tip]))
        walk(parsed["tree"], "")
    path = args["path"]
    if path not in flat:
        raise ValueError(f"File not found: {path}")
    blob = repo.objects[flat[path]]
    data = obj.load_raw("blob", blob)
    return {"sha": flat[path], "path": path,
            "content": base64.b64encode(data).decode(),
            "encoding": "base64"}


@tool("create_or_update_file",
      "Create or update a file in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "path": {"type": "string"},
                      "content": {"type": "string"},
                      "message": {"type": "string"},
                      "branch": {"type": "string"},
                      "sha": {"type": "string"}},
       "required": ["owner", "repo", "path", "content", "message"]})
def _t_write_file(forge, args):
    import base64
    repo = _repo_arg(forge, args)
    branch = args.get("branch", repo.default_branch)
    data = base64.b64decode(args["content"].encode())
    tip = repo.refs.get("refs/heads/" + branch, "")
    flat: dict[str, str] = {}

    def walk(tree_oid: str, prefix: str) -> None:
        blob = repo.objects.get(tree_oid)
        if blob is None:
            return
        for mode, name, child in obj.decode_tree(
                obj.load_raw("tree", blob)):
            path = prefix + name
            if mode == "40000":
                walk(child, path + "/")
            else:
                flat[path] = child

    if tip and tip in repo.objects:
        parsed = obj.decode_commit(obj.load_raw("commit", repo.objects[tip]))
        walk(parsed["tree"], "")
    flat[args["path"]] = repo.store_object("blob", data)

    def build(prefix: str) -> str:
        entries = []
        direct: dict[str, str] = {}
        subs: dict[str, dict] = {}
        for path, oid in flat.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                head, _ = rest.split("/", 1)
                subs.setdefault(head, {})[path] = oid
            else:
                direct[rest] = oid
        for name, oid in direct.items():
            entries.append(("100644", name, oid))
        for name in subs:
            entries.append(("40000", name, build(prefix + name + "/")))
        return repo.store_object(
            "tree", obj.encode_tree(entries))

    tree_oid = build("")
    parents = [tip] if tip else []
    commit = obj.encode_commit(tree_oid, parents, args["message"])
    commit_oid = obj.object_id("commit", commit)
    repo.objects[commit_oid] = obj.raw_bytes("commit", commit)
    repo.update_ref("refs/heads/" + branch, commit_oid)
    return {"commit": {"sha": commit_oid},
            "content": {"sha": flat[args["path"]], "path": args["path"]}}


@tool("list_commits",
      "List commits in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "sha": {"type": "string"}},
       "required": ["owner", "repo"]})
def _t_list_commits(forge, args):
    repo = _repo_arg(forge, args)
    branch = args.get("sha", repo.default_branch)
    tip = repo.refs.get("refs/heads/" + branch, branch)
    items = []
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop(0)
        if not oid or oid in seen or oid not in repo.objects:
            continue
        seen.add(oid)
        blob = repo.objects[oid]
        if blob.split(b" ", 1)[0] != b"commit":
            continue
        parsed = obj.decode_commit(obj.load_raw("commit", blob))
        items.append({"sha": oid, "message": parsed["message"],
                      "tree": parsed["tree"], "parents": parsed["parents"]})
        stack.extend(parsed["parents"])
    return {"total_count": len(items), "items": items}


@tool("get_commit",
      "Get a commit from a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "sha": {"type": "string"}},
       "required": ["owner", "repo", "sha"]})
def _t_get_commit(forge, args):
    repo = _repo_arg(forge, args)
    oid = args["sha"]
    blob = repo.objects.get(oid)
    if blob is None or blob.split(b" ", 1)[0] != b"commit":
        raise ValueError(f"Commit not found: {oid}")
    parsed = obj.decode_commit(obj.load_raw("commit", blob))
    return {"sha": oid, "message": parsed["message"],
            "tree": parsed["tree"], "parents": parsed["parents"]}


@tool("list_branches",
      "List branches in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"}},
       "required": ["owner", "repo"]})
def _t_list_branches(forge, args):
    repo = _repo_arg(forge, args)
    items = [{"name": ref[len("refs/heads/"):],
              "commit": {"sha": oid}}
             for ref, oid in sorted(repo.refs.items())
             if ref.startswith("refs/heads/")]
    return {"total_count": len(items), "items": items}


@tool("create_branch",
      "Create a branch in a repository.",
      {"type": "object",
       "properties": {"owner": {"type": "string"},
                      "repo": {"type": "string"},
                      "branch": {"type": "string"},
                      "from_branch": {"type": "string"}},
       "required": ["owner", "repo", "branch"]})
def _t_create_branch(forge, args):
    repo = _repo_arg(forge, args)
    source = args.get("from_branch", repo.default_branch)
    oid = repo.refs.get("refs/heads/" + source)
    if oid is None:
        raise ValueError(f"Branch not found: {source}")
    repo.update_ref("refs/heads/" + args["branch"], oid)
    return {"name": args["branch"], "commit": {"sha": oid}}


# --- JSON-RPC dispatch ------------------------------------------------------

def handle(forge: model.ForgeState, body: bytes) -> tuple[int, bytes]:
    """One JSON-RPC request (or a batch). Returns ``(status, json)``."""
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        return 400, json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"}}).encode()
    if isinstance(payload, list):
        results = [handle_one(forge, item) for item in payload]
        results = [r for r in results if r is not None]
        return 200, json.dumps(results).encode()
    result = handle_one(forge, payload)
    if result is None:
        return 202, b""
    return 200, json.dumps(result).encode()


def handle_one(forge: model.ForgeState, req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "github-forge",
                                          "version": "1.0.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        tools = [{k: v[k] for k in ("name", "description", "inputSchema")}
                 for v in TOOLS.values()]
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if entry is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602,
                              "message": f"unknown tool: {name}"}}
        try:
            value = entry["handler"](forge, args)
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text",
                                            "text": f"Error: {exc}"}],
                               "isError": True}}
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(value)}]}}
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}
