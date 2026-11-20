"""GraphQL endpoint: lexer, parser, and a resolver-registry engine.

Fields are resolved through ``RESOLVERS``, a module-level registry
mapping ``(type_name, field_name)`` to a function. A field with no
registered resolver falls back to attribute/dict lookup on the value,
so plain data flows through without ceremony.
"""

from __future__ import annotations

import hashlib
import re

from . import model, objects as obj
from .shapes import API, WEB

# --- lexer ----------------------------------------------------------------

_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_INT = re.compile(r"-?(0|[1-9][0-9]*)")
_FLOAT = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_PUNCT = set("!$&():=[]{}|=@.")


class Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value):
        self.kind = kind
        self.value = value


class Lexer:
    """Turns a query document into a token stream."""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0

    def tokens(self) -> list[Token]:
        out: list[Token] = []
        src, n = self.source, len(self.source)
        i = 0
        while i < n:
            ch = src[i]
            if ch in " \t\n\r,":
                i += 1
            elif ch == "#":
                while i < n and src[i] != "\n":
                    i += 1
            elif ch == '"':
                if src.startswith('"""', i):
                    end = src.find('"""', i + 3)
                    if end < 0:
                        raise ValueError("unterminated block string")
                    out.append(Token("string", src[i + 3:end]))
                    i = end + 3
                else:
                    j = i + 1
                    buf = []
                    while j < n and src[j] != '"':
                        if src[j] == "\\":
                            j += 1
                            buf.append(src[j])
                        else:
                            buf.append(src[j])
                        j += 1
                    out.append(Token("string", "".join(buf)))
                    i = j + 1
            elif ch in _PUNCT:
                out.append(Token(ch, ch))
                i += 1
            elif ch.isdigit() or (ch == "-" and i + 1 < n
                                  and src[i + 1].isdigit()):
                m = _FLOAT.match(src, i)
                text = m.group(0)
                out.append(Token("float" if "." in text or "e" in text
                                 or "E" in text else "int", text))
                i = m.end()
            elif ch == "_" or ch.isalpha():
                m = _NAME.match(src, i)
                out.append(Token("name", m.group(0)))
                i = m.end()
            else:
                raise ValueError(f"unexpected character {ch!r}")
        out.append(Token("eof", None))
        return out


# --- parser ---------------------------------------------------------------

class Parser:
    """Recursive-descent parser producing a small dict AST."""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def take(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.take()
        if tok.kind != kind:
            raise ValueError(f"expected {kind}, got {tok.kind}")
        return tok

    def document(self) -> dict:
        defs = []
        while self.peek().kind != "eof":
            defs.append(self.definition())
        return {"definitions": defs}

    def definition(self) -> dict:
        tok = self.peek()
        if tok.kind == "name" and tok.value in ("query", "mutation",
                                                "subscription"):
            op = self.take().value
            name = None
            if self.peek().kind == "name":
                name = self.take().value
            variables = {}
            if self.peek().kind == "(":
                variables = self.variable_defs()
            directives = self.directives()
            selection = self.selection_set()
            return {"kind": "operation", "operation": op, "name": name,
                    "variables": variables, "directives": directives,
                    "selection": selection}
        if tok.kind == "{":
            return {"kind": "operation", "operation": "query", "name": None,
                    "variables": {}, "directives": [],
                    "selection": self.selection_set()}
        if tok.kind == "name" and tok.value == "fragment":
            self.take()
            name = self.expect("name").value
            self.expect("name")  # "on"
            type_name = self.expect("name").value
            return {"kind": "fragment", "name": name,
                    "type": type_name,
                    "selection": self.selection_set()}
        raise ValueError("unsupported definition")

    def _parse_type(self) -> str:
        """A GraphQL type: ``Name``, ``Name!``, ``[T]`` or ``[T]!``."""
        if self.peek().kind == "[":
            self.take()
            inner = self._parse_type()
            self.expect("]")
            result = "[" + inner + "]"
        else:
            result = self.expect("name").value
        if self.peek().kind == "!":
            self.take()
            result += "!"
        return result

    def variable_defs(self) -> dict:
        self.expect("(")
        out = {}
        while self.peek().kind != ")":
            self.expect("$")
            name = self.expect("name").value
            self.expect(":")
            self._parse_type()
            if self.peek().kind == "=":
                self.take()
                out[name] = self.value()
            else:
                out[name] = None
        self.expect(")")
        return out

    def directives(self) -> list:
        out = []
        while self.peek().kind == "@":
            self.take()
            name = self.expect("name").value
            args = {}
            if self.peek().kind == "(":
                args = self.arguments()
            out.append({"name": name, "args": args})
        return out

    def selection_set(self) -> list:
        self.expect("{")
        fields = []
        while self.peek().kind != "}":
            if self.peek().kind == ".":
                fields.append(self.fragment())
            else:
                fields.append(self.field())
        self.expect("}")
        return fields

    def fragment(self) -> dict:
        """``... on Type { ... }`` or ``...Name``."""
        self.expect(".")
        self.expect(".")
        self.expect(".")
        if self.peek().kind == "name" and self.peek().value == "on":
            self.take()  # "on"
            type_name = self.expect("name").value
            selection = self.selection_set()
            return {"kind": "inline_fragment", "type": type_name,
                    "selection": selection}
        name = self.expect("name").value
        directives = self.directives()
        return {"kind": "fragment_spread", "name": name,
                "directives": directives}

    def field(self) -> dict:
        alias = None
        name = self.expect("name").value
        if self.peek().kind == ":":
            self.take()
            alias = name
            name = self.expect("name").value
        args = {}
        if self.peek().kind == "(":
            args = self.arguments()
        directives = self.directives()
        selection = []
        if self.peek().kind == "{":
            selection = self.selection_set()
        return {"alias": alias or name, "name": name, "args": args,
                "directives": directives, "selection": selection}

    def arguments(self) -> dict:
        self.expect("(")
        out = {}
        while self.peek().kind != ")":
            name = self.expect("name").value
            self.expect(":")
            out[name] = self.value()
        self.expect(")")
        return out

    def value(self):
        tok = self.take()
        if tok.kind == "int":
            return int(tok.value)
        if tok.kind == "float":
            return float(tok.value)
        if tok.kind == "string":
            return tok.value
        if tok.kind == "name":
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return {"enum": tok.value}
        if tok.kind == "$":
            return {"var": self.expect("name").value}
        if tok.kind == "[":
            items = []
            while self.peek().kind != "]":
                items.append(self.value())
            self.expect("]")
            return items
        if tok.kind == "{":
            out = {}
            while self.peek().kind != "}":
                key = self.expect("name").value
                self.expect(":")
                out[key] = self.value()
            self.expect("}")
            return out
        raise ValueError(f"unexpected value token {tok.kind}")


# --- engine ---------------------------------------------------------------

RESOLVERS: dict[tuple[str, str], object] = {}


def resolve(type_name: str, field_name: str):
    """Register a resolver for ``Type.field``."""
    def wrap(fn):
        RESOLVERS[(type_name, field_name)] = fn
        return fn
    return wrap


def _cursor(index: int) -> str:
    import base64
    return base64.b64encode(f"cursor:{index}".encode()).decode()


def _connection(items: list, args: dict, render) -> dict:
    first = args.get("first")
    after = args.get("after")
    start = 0
    if after:
        import base64
        try:
            decoded = base64.b64decode(after.encode()).decode()
            start = int(decoded.split(":")[1]) + 1
        except (ValueError, IndexError):
            start = 0
    window = items[start:]
    if first is not None:
        window = window[:first]
    edges = [{"node": render(node), "cursor": _cursor(start + i)}
             for i, node in enumerate(window)]
    return {"edges": edges,
            "nodes": [render(node) for node in window],
            "totalCount": len(items),
            "pageInfo": {"hasNextPage": start + len(window) < len(items),
                         "hasPreviousPage": start > 0,
                         "startCursor": edges[0]["cursor"] if edges else None,
                         "endCursor": edges[-1]["cursor"] if edges else None}}


class Engine:
    """Executes a parsed document against one forge."""

    def __init__(self, forge: model.ForgeState, variables: dict | None = None):
        self.forge = forge
        self.variables = variables or {}
        self.errors: list[dict] = []
        self.fragments: dict[str, dict] = {}

    def run(self, doc: dict) -> dict:
        data: dict = {}
        self.errors = []
        self.fragments = {d["name"]: d for d in doc["definitions"]
                          if d["kind"] == "fragment"}
        for definition in doc["definitions"]:
            if definition["kind"] != "operation":
                continue
            root: dict = {}
            for field in definition["selection"]:
                try:
                    root[field["alias"]] = self._field(
                        "Query", None, field, definition)
                except Exception as exc:  # noqa: BLE001 - surfaced as error
                    self.errors.append({"message": str(exc)})
                    root[field["alias"]] = None
            data.update(root)
        out: dict = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    # -- value resolution ---------------------------------------------------

    def _arg(self, value):
        if isinstance(value, dict) and "var" in value:
            return self.variables.get(value["var"])
        if isinstance(value, dict) and "enum" in value:
            return value["enum"]
        if isinstance(value, list):
            return [self._arg(v) for v in value]
        if isinstance(value, dict):
            return {k: self._arg(v) for k, v in value.items()}
        return value

    def _args(self, field: dict) -> dict:
        return {k: self._arg(v) for k, v in field["args"].items()}

    def _field(self, type_name: str, value, field: dict,
               definition: dict):
        args = self._args(field)
        key = (type_name, field["name"])
        if key in RESOLVERS:
            result = RESOLVERS[key](self, value, args)
        elif isinstance(value, dict):
            # Partial shapes (connections, refs) return null for a
            # field they do not carry; the strict check below covers
            # real model objects.
            result = value.get(field["name"])
        elif value is not None:
            if not hasattr(value, field["name"]):
                raise ValueError(
                    f'Cannot query field "{field["name"]}" on type '
                    f'"{type_name}".')
            result = getattr(value, field["name"], None)
        else:
            result = None
        if field["selection"] and result is not None:
            return self._select(result, field["selection"], definition)
        return result

    def _select(self, value, selection: list, definition: dict):
        if isinstance(value, list):
            return [self._select(item, selection, definition)
                    for item in value]
        type_name = self._typename(value)
        out = {}
        for field in selection:
            if field.get("kind") == "inline_fragment":
                if field["type"] != type_name:
                    continue
                out.update(self._select(value, field["selection"],
                                        definition))
                continue
            if field.get("kind") == "fragment_spread":
                frag = self.fragments.get(field["name"])
                if frag is None:
                    continue
                if frag["type"] != type_name:
                    continue
                out.update(self._select(value, frag["selection"],
                                        definition))
                continue
            if field["name"] == "__typename":
                out[field["alias"]] = type_name
                continue
            try:
                out[field["alias"]] = self._field(type_name, value, field,
                                                  definition)
            except Exception as exc:  # noqa: BLE001 - surfaced as error
                self.errors.append({"message": str(exc)})
                out[field["alias"]] = None
        return out

    @staticmethod
    def _typename(value) -> str:
        if isinstance(value, model.Repository):
            return "Repository"
        if isinstance(value, model.Issue):
            return "Issue"
        if isinstance(value, model.PullRequest):
            return "PullRequest"
        if isinstance(value, model.Account):
            return "User"
        if isinstance(value, dict):
            return value.get("__typename", "Unknown")
        return "Unknown"

    # -- context helpers ----------------------------------------------------

    def repo_of(self, owner: str, name: str) -> model.Repository:
        repo = self.forge.find_repo(owner, name)
        if repo is None:
            raise ValueError("Could not resolve to a Repository with the name of "
                             f"'{owner}/{name}'.")
        return repo

    def actor(self) -> model.Account:
        return self.forge.account("adaiversen")


# --- Query resolvers ------------------------------------------------------

@resolve("Query", "viewer")
def _viewer(engine: Engine, _value, _args):
    return engine.actor()


@resolve("Query", "repository")
def _repository(engine: Engine, _value, args):
    # An unknown repository is null, not an error.
    return engine.forge.find_repo(args.get("owner", ""),
                                  args.get("name", ""))


@resolve("Query", "node")
def _node(engine: Engine, _value, args):
    node_id = args.get("id", "")
    for repo in engine.forge.repositories.values():
        if repo.node == node_id:
            return repo
        for issue in repo.issues.values():
            if issue.node == node_id:
                return issue
        for pr in repo.pulls.values():
            if pr.node == node_id:
                return pr
    return None


@resolve("Query", "search")
def _search(engine: Engine, _value, args):
    query = (args.get("query") or "").lower()
    kind = args.get("type", "ISSUE")
    items = []
    for repo in engine.forge.repositories.values():
        if kind in ("REPOSITORY",):
            hay = f"{repo.owner}/{repo.name} {repo.description}".lower()
            if not query or query in hay:
                items.append(repo)
        else:
            for issue in repo.issues.values():
                if not query or query in f"{issue.title} {issue.body}".lower():
                    items.append(issue)
            for pr in repo.pulls.values():
                if not query or query in f"{pr.title} {pr.body}".lower():
                    items.append(pr)
    first = args.get("first")
    window = items[:first] if first is not None else items
    return {"edges": [{"node": item, "cursor": _cursor(i)}
                      for i, item in enumerate(window)],
            "nodes": window,
            "issueCount": len(items),
            "repositoryCount": len(items),
            "userCount": 0,
            "pageInfo": {"hasNextPage": False, "hasPreviousPage": False,
                         "startCursor": None, "endCursor": None}}


@resolve("Query", "rateLimit")
def _rate_limit(engine: Engine, _value, _args):
    return {"limit": 5000, "cost": 1, "remaining": 4999,
            "resetAt": "2026-11-20T00:00:00Z", "used": 1,
            "nodeCount": 0}


@resolve("User", "databaseId")
def _u_database_id(engine: Engine, user, _args):
    if isinstance(user, model.Account):
        return user.uid
    if isinstance(user, dict):
        login = user.get("login", "")
        return engine.forge.account(login).uid if login else 0
    return 0


@resolve("User", "id")
def _u_id(engine: Engine, user, _args):
    if isinstance(user, model.Account):
        return user.node
    if isinstance(user, dict):
        return user.get("id") or user.get("node_id", "")
    return ""


@resolve("User", "login")
def _u_login(engine: Engine, user, _args):
    if isinstance(user, model.Account):
        return user.login
    if isinstance(user, dict):
        return user.get("login", "")
    return ""


@resolve("User", "name")
def _u_name(engine: Engine, user, _args):
    return _u_login(engine, user, _args)


@resolve("User", "url")
def _u_url(engine: Engine, user, _args):
    login = _u_login(engine, user, _args)
    return f"https://github.com/{login}" if login else ""


@resolve("User", "avatarUrl")
def _u_avatar(engine: Engine, user, _args):
    if isinstance(user, model.Account):
        return (f"https://avatars.githubusercontent.com/u/{user.uid}"
                "?v=4")
    return ""


@resolve("IssueComment", "id")
def _c_id(engine: Engine, comment, _args):
    # The global id is the opaque node id, not the integer row id.
    if isinstance(comment, dict):
        return comment.get("node_id") or str(comment.get("id", ""))
    return getattr(comment, "node_id", "") or ""


# --- Repository resolvers -------------------------------------------------

def _repo_owner(engine: Engine, repo: model.Repository, _args):
    return engine.forge.account(repo.owner)


@resolve("Repository", "owner")
def _r_owner(engine, repo, args):
    return _repo_owner(engine, repo, args)


@resolve("Repository", "databaseId")
def _r_database_id(engine, repo, _args):
    return repo.rid


@resolve("Repository", "viewerPermission")
def _r_viewer_permission(engine, repo, _args):
    return "ADMIN"


@resolve("Repository", "issue")
def _r_issue(engine, repo, args):
    number = args.get("number")
    if number is None:
        return None
    return repo.issues.get(int(number))


@resolve("Repository", "pullRequest")
def _r_pull_request(engine, repo, args):
    number = args.get("number")
    if number is None:
        return None
    return repo.pulls.get(int(number))


@resolve("Repository", "nameWithOwner")
def _r_full(engine, repo, _args):
    return f"{repo.owner}/{repo.name}"


@resolve("Repository", "defaultBranchRef")
def _r_default_branch(engine, repo, _args):
    oid = repo.refs.get("refs/heads/" + repo.default_branch)
    if oid is None:
        return None
    return {"name": repo.default_branch,
            "prefix": "refs/heads/",
            "target": {"__typename": "Commit", "oid": oid,
                       "repo": repo}}


@resolve("Repository", "issues")
def _r_issues(engine, repo, args):
    items = sorted(repo.issues.values(), key=lambda i: i.number)
    states = args.get("states")
    if states:
        items = [i for i in items if i.state.lower() in
                 [s.lower() for s in states]]
    labels = args.get("labels")
    if labels:
        items = [i for i in items
                 if any(l["name"] in labels for l in i.labels)]
    order_by = args.get("orderBy") or {}
    if isinstance(order_by, dict) and order_by.get("direction") == "DESC":
        items = list(reversed(items))
    return _connection(items, args, lambda i: i)


@resolve("Repository", "pullRequests")
def _r_pulls(engine, repo, args):
    items = sorted(repo.pulls.values(), key=lambda p: p.number)
    states = args.get("states")
    if states:
        items = [p for p in items if p.state.lower() in
                 [s.lower() for s in states]]
    order_by = args.get("orderBy") or {}
    if isinstance(order_by, dict) and order_by.get("direction") == "DESC":
        items = list(reversed(items))
    return _connection(items, args, lambda p: p)


@resolve("Repository", "labels")
def _r_labels(engine, repo, args):
    items = [{"__typename": "Label", "name": l["name"],
              "color": l["color"], "description": l.get("description", ""),
              "id": "LA_kwDO" + hashlib.sha1(
                  l["name"].encode()).hexdigest()[:20]}
             for l in repo.labels]
    return _connection(items, args, lambda x: x)


@resolve("Repository", "milestones")
def _r_milestones(engine, repo, args):
    items = [{"__typename": "Milestone", **m} for m in repo.milestones]
    return _connection(items, args, lambda x: x)


@resolve("Repository", "refs")
def _r_refs(engine, repo, args):
    prefix = args.get("refPrefix", "")
    items = []
    for ref, oid in sorted(repo.refs.items()):
        if prefix and not ref.startswith(prefix):
            continue
        name = ref[len(prefix):] if prefix else ref
        items.append({"__typename": "Ref", "name": name, "prefix": prefix,
                      "target": {"__typename": "Commit", "oid": oid,
                                 "repo": repo}})
    return _connection(items, args, lambda x: x)


@resolve("Repository", "object")
def _r_object(engine, repo, args):
    expression = args.get("expression", "")
    if ":" in expression:
        branch, _, path = expression.partition(":")
    else:
        branch, path = expression, ""
    oid = repo.refs.get("refs/heads/" + branch)
    if oid is None:
        oid = repo.refs.get("refs/tags/" + branch)
    if oid is None:
        return None
    if not path:
        return {"__typename": "Commit", "oid": oid, "repo": repo}
    flat = {}
    _flatten(engine, repo, oid, flat)
    blob_oid = flat.get(path)
    if blob_oid is None:
        return None
    return {"__typename": "Blob", "oid": blob_oid, "repo": repo,
            "isBinary": False, "byteSize": 0, "text": ""}


def _flatten(engine, repo, commit_oid, out):
    blob = repo.objects.get(commit_oid)
    if blob is None:
        return
    parsed = obj.decode_commit(obj.load_raw("commit", blob))
    _flatten_tree(engine, repo, parsed["tree"], "", out)


def _flatten_tree(engine, repo, tree_oid, prefix, out):
    blob = repo.objects.get(tree_oid)
    if blob is None:
        return
    for mode, name, child in obj.decode_tree(obj.load_raw("tree", blob)):
        path = prefix + name
        if mode == "40000":
            _flatten_tree(engine, repo, child, path + "/", out)
        else:
            out[path] = child


@resolve("Repository", "createdAt")
def _r_created(engine, repo, _args):
    return repo.created_at


@resolve("Repository", "updatedAt")
def _r_updated(engine, repo, _args):
    return repo.updated_at


@resolve("Repository", "pushedAt")
def _r_pushed(engine, repo, _args):
    return repo.pushed_at


@resolve("Repository", "isPrivate")
def _r_private(engine, repo, _args):
    return repo.private


@resolve("Repository", "forkCount")
def _r_forks(engine, repo, _args):
    return 0


@resolve("Repository", "stargazerCount")
def _r_stars(engine, repo, _args):
    return 0


@resolve("Repository", "watcherCount")
def _r_watchers(engine, repo, _args):
    return 0


@resolve("Repository", "diskUsage")
def _r_disk(engine, repo, _args):
    return sum(len(b) for b in repo.objects.values()) // 1024


@resolve("Repository", "hasIssuesEnabled")
def _r_has_issues(engine, repo, _args):
    return True


@resolve("Repository", "hasProjectsEnabled")
def _r_has_projects(engine, repo, _args):
    return True


@resolve("Repository", "hasWikiEnabled")
def _r_has_wiki(engine, repo, _args):
    return True


@resolve("Repository", "url")
def _r_url(engine, repo, _args):
    return f"{WEB}/{repo.owner}/{repo.name}"


@resolve("Repository", "id")
def _r_id(engine, repo, _args):
    return repo.node


# --- Issue resolvers ------------------------------------------------------

@resolve("Issue", "author")
def _i_author(engine, issue, _args):
    return engine.forge.account(issue.author)


@resolve("Issue", "databaseId")
def _i_database_id(engine, issue, _args):
    return issue.number


@resolve("Issue", "state")
def _i_state(engine, issue, _args):
    return issue.state.upper()


@resolve("Issue", "labels")
def _i_labels(engine, issue, args):
    items = [{"__typename": "Label", "name": l["name"],
              "color": l.get("color", "ededed"),
              "description": l.get("description", ""),
              "id": "LA_kwDO" + hashlib.sha1(
                  l["name"].encode()).hexdigest()[:20]}
             for l in issue.labels]
    return _connection(items, args, lambda x: x)


@resolve("Issue", "assignees")
def _i_assignees(engine, issue, args):
    items = ([engine.forge.account(issue.assignee)]
             if issue.assignee else [])
    return _connection(items, args, lambda x: x)


@resolve("Issue", "milestone")
def _i_milestone(engine, issue, _args):
    if issue.milestone is None:
        return None
    return {"__typename": "Milestone", **issue.milestone}


@resolve("Issue", "comments")
def _i_comments(engine, issue, args):
    items = [{"__typename": "IssueComment", **c} for c in issue.comments]
    return _connection(items, args, lambda x: x)


@resolve("Issue", "id")
def _i_id(engine, issue, _args):
    return issue.node


@resolve("Issue", "url")
def _i_url(engine, issue, _args):
    return ""


@resolve("Issue", "reactions")
def _i_reactions(engine, issue, args):
    return _connection([], args, lambda x: x)


# --- PullRequest resolvers ------------------------------------------------

@resolve("PullRequest", "author")
def _p_author(engine, pr, _args):
    return engine.forge.account(pr.author)


@resolve("PullRequest", "databaseId")
def _p_database_id(engine, pr, _args):
    return pr.number


@resolve("PullRequest", "state")
def _p_state(engine, pr, _args):
    if pr.merged:
        return "MERGED"
    return pr.state.upper()


@resolve("PullRequest", "headRefName")
def _p_head_ref_name(engine, pr, _args):
    return pr.head


@resolve("PullRequest", "baseRefName")
def _p_base_ref_name(engine, pr, _args):
    return pr.base


def _repo_for_pr(engine, pr):
    """The repository that owns this pull request, if any."""
    for repo in engine.forge.repositories.values():
        if repo.pulls.get(pr.number) is pr:
            return repo
    return None


@resolve("PullRequest", "mergeable")
def _p_mergeable(engine, pr, _args):
    repo = _repo_for_pr(engine, pr)
    if repo is None:
        return "UNKNOWN"
    if pr.state != "open":
        return "UNKNOWN"
    return "CONFLICTING" if repo.merge_conflict(pr) else "MERGEABLE"


@resolve("PullRequest", "files")
def _p_files(engine, pr, args):
    repo = _repo_for_pr(engine, pr)
    if repo is None:
        return _connection([], args, lambda x: x)
    base_tree = repo._flat_tree(pr.base_sha)
    head_tree = repo._flat_tree(pr.head_sha)
    items = []
    for path in sorted(set(base_tree) | set(head_tree)):
        if base_tree.get(path) == head_tree.get(path):
            continue
        old_data = _blob_bytes(repo, base_tree.get(path))
        new_data = _blob_bytes(repo, head_tree.get(path))
        added, removed = _line_diff(old_data, new_data)
        items.append({"__typename": "PullRequestFile", "path": path,
                      "additions": added, "deletions": removed,
                      "changeType": ("ADDED" if path not in base_tree
                                    else "REMOVED" if path not in head_tree
                                    else "MODIFIED")})
    return _connection(items, args, lambda x: x)


def _blob_bytes(repo, oid):
    if not oid:
        return b""
    blob = repo.objects.get(oid)
    if blob is None:
        return b""
    return obj.load_raw("blob", blob)


def _line_diff(old: bytes, new: bytes):
    import difflib
    old_lines = old.decode(errors="replace").splitlines()
    new_lines = new.decode(errors="replace").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


@resolve("PullRequest", "headRef")
def _p_head_ref(engine, pr, _args):
    return {"__typename": "Ref", "name": pr.head,
            "prefix": "refs/heads/",
            "target": {"__typename": "Commit", "oid": pr.head_sha,
                       "repo": None}}


@resolve("PullRequest", "baseRef")
def _p_base_ref(engine, pr, _args):
    return {"__typename": "Ref", "name": pr.base,
            "prefix": "refs/heads/",
            "target": {"__typename": "Commit", "oid": pr.base_sha,
                       "repo": None}}


@resolve("PullRequest", "headRefOid")
def _p_head_oid(engine, pr, _args):
    return pr.head_sha


@resolve("PullRequest", "baseRefOid")
def _p_base_oid(engine, pr, _args):
    return pr.base_sha


@resolve("PullRequest", "merged")
def _p_merged(engine, pr, _args):
    return pr.merged


@resolve("PullRequest", "mergedAt")
def _p_merged_at(engine, pr, _args):
    return pr.merged_at


@resolve("PullRequest", "isDraft")
def _p_draft(engine, pr, _args):
    return pr.draft


@resolve("PullRequest", "labels")
def _p_labels(engine, pr, args):
    items = [{"__typename": "Label", "name": l["name"],
              "color": l.get("color", "ededed"),
              "description": l.get("description", ""),
              "id": "LA_kwDO" + hashlib.sha1(
                  l["name"].encode()).hexdigest()[:20]}
             for l in pr.labels]
    return _connection(items, args, lambda x: x)


@resolve("PullRequest", "assignees")
def _p_assignees(engine, pr, args):
    items = ([engine.forge.account(pr.assignee)] if pr.assignee else [])
    return _connection(items, args, lambda x: x)


@resolve("PullRequest", "milestone")
def _p_milestone(engine, pr, _args):
    if pr.milestone is None:
        return None
    return {"__typename": "Milestone", **pr.milestone}


@resolve("PullRequest", "comments")
def _p_comments(engine, pr, args):
    items = [{"__typename": "IssueComment", **c} for c in pr.comments]
    return _connection(items, args, lambda x: x)


@resolve("PullRequest", "reviews")
def _p_reviews(engine, pr, args):
    return _connection([], args, lambda x: x)


@resolve("PullRequest", "id")
def _p_id(engine, pr, _args):
    return pr.node


@resolve("PullRequest", "url")
def _p_url(engine, pr, _args):
    return ""


# --- Commit / Blob / Tag resolvers ----------------------------------------

@resolve("Commit", "oid")
def _c_oid(engine, commit, _args):
    return commit["oid"]


@resolve("Commit", "message")
def _c_message(engine, commit, _args):
    repo = commit.get("repo")
    blob = repo.objects.get(commit["oid"]) if repo else None
    if blob is None:
        return ""
    return obj.decode_commit(obj.load_raw("commit", blob))["message"]


@resolve("Commit", "author")
def _c_author(engine, commit, _args):
    return {"__typename": "GitActor", "name": "A U Thor",
            "email": "author@example.com",
            "date": obj.FIXED_DATE + "T00:00:00Z"}


@resolve("Commit", "committer")
def _c_committer(engine, commit, _args):
    return {"__typename": "GitActor", "name": "C O Mitter",
            "email": "committer@example.com",
            "date": obj.FIXED_DATE + "T00:00:00Z"}


@resolve("Commit", "tree")
def _c_tree(engine, commit, _args):
    repo = commit.get("repo")
    blob = repo.objects.get(commit["oid"]) if repo else None
    if blob is None:
        return {"__typename": "Tree", "oid": "", "repo": repo}
    parsed = obj.decode_commit(obj.load_raw("commit", blob))
    return {"__typename": "Tree", "oid": parsed["tree"], "repo": repo}


@resolve("Commit", "parents")
def _c_parents(engine, commit, args):
    repo = commit.get("repo")
    blob = repo.objects.get(commit["oid"]) if repo else None
    if blob is None:
        return _connection([], args, lambda x: x)
    parsed = obj.decode_commit(obj.load_raw("commit", blob))
    items = [{"__typename": "Commit", "oid": p, "repo": repo}
             for p in parsed["parents"]]
    return _connection(items, args, lambda x: x)


@resolve("Blob", "oid")
def _b_oid(engine, blob, _args):
    return blob["oid"]


@resolve("Blob", "text")
def _b_text(engine, blob, _args):
    repo = blob.get("repo")
    raw = repo.objects.get(blob["oid"]) if repo else None
    if raw is None:
        return ""
    return obj.load_raw("blob", raw).decode(errors="replace")


@resolve("Blob", "byteSize")
def _b_size(engine, blob, _args):
    repo = blob.get("repo")
    raw = repo.objects.get(blob["oid"]) if repo else None
    return len(obj.load_raw("blob", raw)) if raw else 0


@resolve("Blob", "isBinary")
def _b_binary(engine, blob, _args):
    return False


@resolve("Tree", "oid")
def _t_oid(engine, tree, _args):
    return tree["oid"]


@resolve("Tree", "entries")
def _t_entries(engine, tree, args):
    repo = tree.get("repo")
    blob = repo.objects.get(tree["oid"]) if repo else None
    if blob is None:
        return _connection([], args, lambda x: x)
    items = []
    for mode, name, child in obj.decode_tree(obj.load_raw("tree", blob)):
        kind = "Tree" if mode == "40000" else "Blob"
        items.append({"__typename": "TreeEntry", "name": name,
                      "type": kind, "mode": int(mode, 8),
                      "object": {"__typename": kind, "oid": child,
                                 "repo": repo}})
    return _connection(items, args, lambda x: x)


@resolve("Ref", "target")
def _ref_target(engine, ref, _args):
    return ref["target"]


@resolve("Ref", "name")
def _ref_name(engine, ref, _args):
    return ref["name"]


@resolve("Ref", "prefix")
def _ref_prefix(engine, ref, _args):
    return ref["prefix"]


# --- mutations ------------------------------------------------------------

def execute_mutation(engine: Engine, doc: dict) -> dict:
    """Run the single mutation operation in ``doc``."""
    data: dict = {}
    errors: list[dict] = []
    for definition in doc["definitions"]:
        if definition["kind"] != "operation":
            continue
        for field in definition["selection"]:
            try:
                data[field["alias"]] = _mutate(engine, field)
            except Exception as exc:  # noqa: BLE001
                errors.append({"message": str(exc)})
                data[field["alias"]] = None
    out: dict = {"data": data}
    if errors:
        out["errors"] = errors
    return out


def _mutate(engine: Engine, field: dict):
    args = {k: engine._arg(v) for k, v in field["args"].items()}
    name = field["name"]
    selection = field["selection"]
    if name == "createIssue":
        return _m_create_issue(engine, args, selection)
    if name == "updateIssue":
        return _m_update_issue(engine, args, selection)
    if name == "closeIssue":
        return _m_close_issue(engine, args, selection)
    if name == "reopenIssue":
        return _m_reopen_issue(engine, args, selection)
    if name == "addComment":
        return _m_add_comment(engine, args, selection)
    if name == "createPullRequest":
        return _m_create_pull(engine, args, selection)
    if name == "mergePullRequest":
        return _m_merge_pull(engine, args, selection)
    if name == "closePullRequest":
        return _m_close_pull(engine, args, selection)
    if name == "createRepository":
        return _m_create_repo(engine, args, selection)
    if name == "deleteRepository":
        return _m_delete_repo(engine, args, selection)
    if name == "addLabelsToLabelable":
        return _m_add_labels(engine, args, selection)
    if name == "removeLabelsFromLabelable":
        return _m_remove_labels(engine, args, selection)
    if name == "addAssigneesToAssignable":
        return _m_add_assignees(engine, args, selection)
    if name == "createLabel":
        return _m_create_label(engine, args, selection)
    if name == "createMilestone":
        return _m_create_milestone(engine, args, selection)
    if name == "updatePullRequest":
        return _m_update_pull(engine, args, selection)
    raise ValueError(f"unknown mutation {name}")


def _input(args: dict, key: str = "input") -> dict:
    value = args.get(key) or {}
    return value if isinstance(value, dict) else {}


def _pick(engine: Engine, payload, selection: list) -> dict:
    """Select the mutation payload's fields through the engine."""
    out = {}
    for field in selection:
        if field["name"] == "clientMutationId":
            out[field["alias"]] = None
            continue
        value = payload.get(field["name"]) if isinstance(payload, dict) else None
        if field["selection"] and value is not None:
            out[field["alias"]] = engine._select(value, field["selection"],
                                                {})
        else:
            out[field["alias"]] = value
    return out


def _find_by_node(engine: Engine, node_id: str):
    for repo in engine.forge.repositories.values():
        if repo.node == node_id:
            return ("repo", repo)
        for issue in repo.issues.values():
            if issue.node == node_id:
                return ("issue", repo, issue)
        for pr in repo.pulls.values():
            if pr.node == node_id:
                return ("pull", repo, pr)
    return None


def _m_create_issue(engine, args, selection):
    data = _input(args)
    repo_id = data.get("repositoryId", "")
    found = _find_by_node(engine, repo_id)
    if found is None or found[0] != "repo":
        raise ValueError("repository not found")
    repo = found[1]
    issue = repo.open_issue(data.get("title", ""), data.get("body", ""),
                            engine.actor().login,
                            data.get("labelIds", []))
    issue.touch()
    return _pick(engine, {"issue": issue}, selection)


def _m_update_issue(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("id", ""))
    if found is None or found[0] != "issue":
        raise ValueError("issue not found")
    _repo, issue = found[1], found[2]
    if "title" in data:
        issue.title = data["title"]
    if "body" in data:
        issue.body = data["body"]
    if "state" in data:
        issue.state = data["state"].lower()
    issue.touch()
    return _pick(engine, {"issue": issue}, selection)


def _m_close_issue(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("issueId", ""))
    if found is None or found[0] != "issue":
        raise ValueError("issue not found")
    _repo, issue = found[1], found[2]
    issue.state = "closed"
    issue.closed_at = obj.FIXED_DATE + "T00:00:00Z"
    issue.touch()
    return _pick(engine, {"issue": issue}, selection)


def _m_reopen_issue(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("issueId", ""))
    if found is None or found[0] != "issue":
        raise ValueError("issue not found")
    _repo, issue = found[1], found[2]
    issue.state = "open"
    issue.closed_at = None
    issue.touch()
    return _pick(engine, {"issue": issue}, selection)


def _m_add_comment(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("subjectId", ""))
    if found is None or found[0] not in ("issue", "pull"):
        raise ValueError("subject not found")
    _kind, repo, target = found
    comment = {"id": engine.forge.next_id(),
               "node_id": "IC_kwDO" + hashlib.sha1(
                   f"c:{engine.forge._seq}".encode()).hexdigest()[:20],
               "body": data.get("body", ""),
               "author": engine.actor().login,
               "created_at": obj.FIXED_DATE + "T00:00:00Z",
               "updated_at": obj.FIXED_DATE + "T00:00:00Z"}
    target.comments.append(comment)
    target.touch()
    node = {"__typename": "IssueComment", **comment}
    return _pick(engine, {"commentEdge": {"node": node, "cursor": _cursor(0)},
                          "subject": target}, selection)


def _m_create_pull(engine, args, selection):
    data = _input(args)
    repo_id = data.get("repositoryId", "")
    found = _find_by_node(engine, repo_id)
    if found is None or found[0] != "repo":
        raise ValueError("repository not found")
    repo = found[1]
    pr = repo.open_pull(data.get("title", ""), data.get("body", ""),
                        data.get("headRefName", ""),
                        data.get("baseRefName", repo.default_branch),
                        engine.actor().login,
                        draft=bool(data.get("draft", False)))
    return _pick(engine, {"pullRequest": pr}, selection)


def _m_merge_pull(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("pullRequestId", ""))
    if found is None or found[0] != "pull":
        raise ValueError("pull request not found")
    _kind, repo, pr = found
    if pr.state != "open":
        raise ValueError("Pull request is not open")
    head_oid = repo.refs.get("refs/heads/" + pr.head)
    base_oid = repo.refs.get("refs/heads/" + pr.base)
    head_blob = repo.objects.get(head_oid) if head_oid else None
    if head_blob is None:
        raise ValueError("head commit missing")
    head_parsed = obj.decode_commit(obj.load_raw("commit", head_blob))
    method = (data.get("mergeMethod") or "MERGE").upper()
    parents = ([base_oid, head_oid] if method == "MERGE" else [base_oid])
    merged = obj.encode_commit(head_parsed["tree"], parents,
                               data.get("commitHeadline")
                               or f"{pr.title} (#{pr.number})")
    merged_oid = obj.object_id("commit", merged)
    repo.objects[merged_oid] = obj.raw_bytes("commit", merged)
    repo.refs["refs/heads/" + pr.base] = merged_oid
    pr.state = "closed"
    pr.merged = True
    pr.merge_sha = merged_oid
    pr.merged_at = obj.FIXED_DATE + "T00:00:00Z"
    pr.closed_at = obj.FIXED_DATE + "T00:00:00Z"
    pr.touch()
    return _pick(engine, {"pullRequest": pr}, selection)


def _m_close_pull(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("pullRequestId", ""))
    if found is None or found[0] != "pull":
        raise ValueError("pull request not found")
    _kind, repo, pr = found
    pr.state = "closed"
    pr.closed_at = obj.FIXED_DATE + "T00:00:00Z"
    pr.touch()
    return _pick(engine, {"pullRequest": pr}, selection)


def _m_create_repo(engine, args, selection):
    data = _input(args)
    name = data.get("name", "")
    owner = data.get("ownerId", "")
    if not name:
        raise ValueError("name is required")
    login = engine.actor().login
    repo = engine.forge.create_repo(login, name,
                                    description=data.get("description", ""),
                                    private=bool(data.get("visibility") == "PRIVATE"))
    return _pick(engine, {"repository": repo}, selection)


def _m_delete_repo(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("repositoryId", ""))
    if found is None or found[0] != "repo":
        raise ValueError("repository not found")
    repo = found[1]
    engine.forge.delete_repo(repo.owner, repo.name)
    return _pick(engine, {"owner": engine.forge.account(repo.owner)},
                 selection)


def _m_add_labels(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("labelableId", ""))
    if found is None or found[0] not in ("issue", "pull"):
        raise ValueError("labelable not found")
    _kind, repo, target = found
    for label_id in data.get("labelIds", []):
        name = label_id
        if name not in [l["name"] for l in target.labels]:
            target.labels.append({"name": name, "color": "ededed",
                                  "description": ""})
    target.touch()
    return _pick(engine, {"labelable": target}, selection)


def _m_remove_labels(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("labelableId", ""))
    if found is None or found[0] not in ("issue", "pull"):
        raise ValueError("labelable not found")
    _kind, repo, target = found
    drop = set(data.get("labelIds", []))
    target.labels = [l for l in target.labels if l["name"] not in drop]
    target.touch()
    return _pick(engine, {"labelable": target}, selection)


def _m_add_assignees(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("assignableId", ""))
    if found is None or found[0] not in ("issue", "pull"):
        raise ValueError("assignable not found")
    _kind, repo, target = found
    ids = data.get("assigneeIds", [])
    if ids:
        target.assignee = ids[0]
    target.touch()
    return _pick(engine, {"assignable": target}, selection)


def _m_create_label(engine, args, selection):
    data = _input(args)
    repo_id = data.get("repositoryId", "")
    found = _find_by_node(engine, repo_id)
    if found is None or found[0] != "repo":
        raise ValueError("repository not found")
    repo = found[1]
    label = {"name": data.get("name", ""),
             "color": data.get("color", "ededed"),
             "description": data.get("description", "")}
    repo.labels.append(label)
    node = {"__typename": "Label", **label,
            "id": "LA_kwDO" + hashlib.sha1(
                label["name"].encode()).hexdigest()[:20]}
    return _pick(engine, {"label": node}, selection)


def _m_create_milestone(engine, args, selection):
    data = _input(args)
    repo_id = data.get("repositoryId", "")
    found = _find_by_node(engine, repo_id)
    if found is None or found[0] != "repo":
        raise ValueError("repository not found")
    repo = found[1]
    number = repo.next_milestone
    repo.next_milestone += 1
    milestone = {"id": engine.forge.next_id(),
                 "node_id": "MI_kwDO" + hashlib.sha1(
                     f"m:{number}".encode()).hexdigest()[:20],
                 "number": number,
                 "title": data.get("title", ""),
                 "description": data.get("description", ""),
                 "creator": engine.forge.account(engine.actor().login).public(),
                 "open_issues": 0, "closed_issues": 0, "state": "open",
                 "created_at": obj.FIXED_DATE + "T00:00:00Z",
                 "updated_at": obj.FIXED_DATE + "T00:00:00Z",
                 "due_on": data.get("dueOn"),
                 "closed_at": None,
                 "url": "", "html_url": "", "labels_url": ""}
    repo.milestones.append(milestone)
    node = {"__typename": "Milestone", **milestone}
    return _pick(engine, {"milestone": node}, selection)


def _m_update_pull(engine, args, selection):
    data = _input(args)
    found = _find_by_node(engine, data.get("pullRequestId", ""))
    if found is None or found[0] != "pull":
        raise ValueError("pull request not found")
    _kind, repo, pr = found
    if "title" in data:
        pr.title = data["title"]
    if "body" in data:
        pr.body = data["body"]
    if "state" in data:
        pr.state = data["state"].lower()
    if "baseRefName" in data:
        pr.base = data["baseRefName"]
        pr.base_sha = repo.refs.get("refs/heads/" + pr.base, "")
    pr.touch()
    return _pick(engine, {"pullRequest": pr}, selection)


# --- introspection ----------------------------------------------------------

SCHEMA_TYPES = [
    "Query", "Mutation", "Repository", "Issue", "PullRequest", "User",
    "Actor", "Label", "Milestone", "Ref", "Commit", "Tree", "Blob", "Tag",
    "GitActor", "IssueComment", "IssueConnection", "IssueEdge",
    "PullRequestConnection", "PullRequestEdge", "LabelConnection",
    "LabelEdge", "MilestoneConnection", "MilestoneEdge", "RefConnection",
    "RefEdge", "CommitConnection", "CommitEdge", "TreeEntry",
    "SearchResultItemConnection", "SearchResultItemEdge", "RateLimit",
    "PageInfo", "RepositoryConnection", "RepositoryEdge", "SearchType",
]


SEARCH_TYPE_VALUES = ["ISSUE", "REPOSITORY", "USER", "DISCUSSION",
                      "COMMIT", "CODE"]


# Field names per GraphQL type, used by introspection. The kit checks
# that Issue and PullRequest expose fields; the engine resolves the
# same names through RESOLVERS plus plain model attributes.
TYPE_FIELDS = {
    "Query": ["viewer", "repository", "node", "search", "rateLimit"],
    "Mutation": ["createIssue", "updateIssue", "closeIssue",
                 "reopenIssue", "addComment", "createPullRequest",
                 "mergePullRequest", "closePullRequest", "createRepository",
                 "deleteRepository", "addLabelsToLabelable",
                 "removeLabelsFromLabelable", "addAssigneesToAssignable",
                 "createLabel", "createMilestone"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "description", "url", "isPrivate", "createdAt",
                   "updatedAt", "pushedAt", "defaultBranchRef",
                   "viewerPermission", "issue", "issues", "pullRequest",
                   "pullRequests", "labels", "milestones", "refs",
                   "object", "forkCount", "stargazerCount", "watcherCount",
                   "diskUsage", "hasIssuesEnabled", "hasProjectsEnabled",
                   "hasWikiEnabled", "homepageUrl", "licenseInfo",
                   "primaryLanguage", "isArchived", "isFork", "visibility",
                   "sshUrl", "cloneUrl", "htmlUrl", "openGraphImageUrl"],
    "Issue": ["id", "databaseId", "number", "title", "body", "bodyText",
              "state", "url", "createdAt", "updatedAt", "closedAt",
              "author", "labels", "assignees", "milestone", "comments",
              "reactions", "repository", "isLocked", "locked",
              "viewerCanUpdate", "timelineItems", "participants"],
    "PullRequest": ["id", "databaseId", "number", "title", "body",
                    "bodyHTML", "state", "url", "createdAt", "updatedAt",
                    "closedAt", "mergedAt", "author", "labels",
                    "assignees", "milestone", "comments", "reviews",
                    "headRef", "baseRef", "headRefName", "baseRefName",
                    "headRefOid", "baseRefOid", "merged", "mergeable",
                    "isDraft", "repository", "isLocked", "files",
                    "commits", "additions", "deletions", "changedFiles",
                    "mergeCommit", "headRepository", "baseRepository"],
    "User": ["id", "databaseId", "login", "name", "url", "avatarUrl",
             "company", "location", "createdAt", "updatedAt"],
    "Actor": ["id", "login", "name", "url", "avatarUrl"],
    "Label": ["id", "name", "color", "description", "url", "isDefault"],
    "Milestone": ["id", "number", "title", "description", "state", "url",
                  "createdAt", "updatedAt", "closedAt", "dueOn", "creator"],
    "Ref": ["id", "name", "prefix", "target"],
    "Commit": ["oid", "id", "message", "messageHeadline", "messageBody",
               "author", "committer", "tree", "parents", "url",
               "committedDate", "authoredDate", "history",
               "abbreviatedOid", "additions", "deletions", "changedFiles"],
    "Tree": ["oid", "id", "entries", "abbreviatedOid"],
    "Blob": ["oid", "id", "text", "byteSize", "isBinary", "isTruncated"],
    "Tag": ["oid", "id", "name", "message", "tagger", "target"],
    "GitActor": ["name", "email", "date", "user", "avatarUrl"],
    "IssueComment": ["id", "body", "bodyText", "bodyHTML", "createdAt",
                     "updatedAt", "author", "url", "viewerDidAuthor",
                     "reactions"],
    "IssueConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "IssueEdge": ["node", "cursor"],
    "PullRequestConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "PullRequestEdge": ["node", "cursor"],
    "LabelConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "LabelEdge": ["node", "cursor"],
    "MilestoneConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "MilestoneEdge": ["node", "cursor"],
    "RefConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "RefEdge": ["node", "cursor"],
    "CommitConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "CommitEdge": ["node", "cursor"],
    "TreeEntry": ["name", "type", "mode", "oid", "object"],
    "SearchResultItemConnection": ["nodes", "edges", "totalCount",
                                   "pageInfo", "issueCount",
                                   "repositoryCount", "userCount",
                                   "codeCount"],
    "SearchResultItemEdge": ["node", "cursor"],
    "RateLimit": ["limit", "cost", "remaining", "resetAt", "used",
                  "nodeCount"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor",
                 "endCursor"],
    "RepositoryConnection": ["nodes", "edges", "totalCount", "pageInfo"],
    "RepositoryEdge": ["node", "cursor"],
}


def _field_info(name: str) -> dict:
    return {"name": name, "description": None, "args": [],
            "type": {"name": "String", "kind": "SCALAR", "ofType": None},
            "isDeprecated": False, "deprecationReason": None}


def _type_info(name: str) -> dict | None:
    if name not in SCHEMA_TYPES:
        return None
    if name == "SearchType":
        return {"name": name, "kind": "ENUM", "description": None,
                "fields": None, "interfaces": None, "possibleTypes": None,
                "enumValues": [{"name": v, "description": None,
                                "isDeprecated": False,
                                "deprecationReason": None}
                               for v in SEARCH_TYPE_VALUES],
                "inputFields": None, "ofType": None}
    return {"name": name, "kind": "OBJECT",
            "description": None,
            "fields": [_field_info(f) for f in TYPE_FIELDS.get(name, [])],
            "interfaces": [], "possibleTypes": None, "enumValues": None,
            "inputFields": None, "ofType": None}


def introspection(engine: Engine, doc: dict) -> dict:
    """Answer ``__schema`` / ``__type`` queries with a static schema."""
    data: dict = {}
    operation = next((d for d in doc["definitions"]
                      if d["kind"] == "operation"), None)
    if operation is None:
        return {"data": {}}
    for field in operation["selection"]:
        # The response key is the alias when one is present.
        key = field["alias"] or field["name"]
        if field["name"] == "__schema":
            data[key] = {
                "queryType": {"name": "Query"},
                "mutationType": {"name": "Mutation"},
                "subscriptionType": None,
                "types": [_type_info(n) for n in SCHEMA_TYPES],
                "directives": []}
        elif field["name"] == "__type":
            name = field["args"].get("name")
            if isinstance(name, dict):
                name = name.get("value")
            data[key] = _type_info(name) if name else None
    return {"data": data}
