# ============================================================================
# YAML-LITE
#
# Subset of YAML, enough for config files: block mappings, block sequences,
# flow sequences/mappings, quoted and plain scalars, comments. Anchors,
# aliases, tags, block scalars (| >) and multiple documents are rejected
# loudly with a line number instead of being silently misparsed.
# ============================================================================

class YamlError(Exception):
    def __init__(self, msg, line=None, text=None):
        self.line, self.text = line, text
        if line:
            msg = f"line {line}: {msg}\n    {text}"
        super().__init__(msg)


class _Line:
    __slots__ = ("no", "indent", "body", "text")

    def __init__(self, no, raw):
        self.no, self.text = no, raw
        stripped = raw.lstrip(" ")
        if stripped.startswith("\t"):
            raise YamlError("tabs are not allowed for indentation", no, raw)
        self.indent = len(raw) - len(stripped)
        self.body = stripped.rstrip("\n\r")


def _strip_comment(text, lineno):
    out, quote, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    if quote:
        raise YamlError("unterminated quoted string", lineno, text)
    return "".join(out).rstrip()


def _split_flow(text, lineno):
    parts, depth, quote, cur = [], 0, None, []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if depth:
        raise YamlError("unbalanced flow collection", lineno, text)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "/": "/"}


def _unescape(body, lineno, text):
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt not in ESCAPES:
                raise YamlError(f"unsupported escape \\{nxt}", lineno, text)
            out.append(ESCAPES[nxt])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scalar(text, lineno):
    text = text.strip()
    if text == "":
        return None
    if text[0] in "&*!":
        raise YamlError("anchors, aliases and tags are not supported", lineno, text)
    if text[0] in "|>":
        raise YamlError("block scalars (| and >) are not supported; use a list", lineno, text)
    if text[0] == '"':
        if len(text) < 2 or not text.endswith('"'):
            raise YamlError("bad double-quoted scalar", lineno, text)
        body = text[1:-1]
        if "\\" in body:
            body = _unescape(body, lineno, text)
        return body
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError("bad single-quoted scalar", lineno, text)
        return text[1:-1].replace("''", "'")
    if text[0] == "[" and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p, lineno) for p in _split_flow(inner, lineno)] if inner else []
    if text[0] == "{" and text.endswith("}"):
        inner = text[1:-1].strip()
        result = {}
        for part in _split_flow(inner, lineno):
            key, sep, val = part.partition(":")
            if not sep:
                raise YamlError(f"expected 'key: value' in flow mapping, got {part!r}", lineno, text)
            result[_key_scalar(key, lineno)] = _scalar(val, lineno)
        return result
    if text in ("~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE", "yes", "on"):
        return True
    if text in ("false", "False", "FALSE", "no", "off"):
        return False
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _key_scalar(text, lineno):
    """A mapping key always stays a string.

    YAML 1.1 turns plain `on`, `off`, `yes` and `no` into booleans, which broke
    `notify.on` (it arrived as the key True). Keys in a config are names, not
    values, so no scalar conversion happens here.
    """
    text = text.strip()
    if not text:
        raise YamlError("empty mapping key", lineno, text)
    if text[0] in "\"'":
        return _scalar(text, lineno)
    if text[0] in "[{&*!|>":
        raise YamlError("bad mapping key", lineno, text)
    return text


def _partition_key(body):
    quote, depth = None, 0
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(body) or body[i + 1] in " \t"):
            return body[:i].strip(), ":", body[i + 1:]
    return body, "", ""


def _is_key_line(text):
    return bool(_partition_key(text)[1]) and not text.startswith(("[", "{"))


class _YamlParser:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        while self.i < len(self.lines) and self.lines[self.i] is None:
            self.i += 1
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse_node(self, indent):
        line = self.peek()
        if line is None or line.indent < indent:
            return None
        if line.body.startswith("- ") or line.body == "-":
            return self.parse_seq(line.indent)
        return self.parse_map(line.indent)

    def parse_seq(self, indent):
        items = []
        while True:
            line = self.peek()
            if line is None or line.indent != indent or not (line.body.startswith("- ") or line.body == "-"):
                if line is not None and line.indent > indent:
                    raise YamlError("bad indentation in sequence", line.no, line.text)
                break
            rest = line.body[1:].strip()
            if rest:
                if _is_key_line(rest):
                    self.lines[self.i] = _Line(line.no, " " * (indent + 2) + rest)
                    items.append(self.parse_map(indent + 2))
                    continue
                items.append(_scalar(_strip_comment(rest, line.no), line.no))
                self.i += 1
                continue
            self.i += 1
            child = self.peek()
            items.append(None if child is None or child.indent <= indent else self.parse_node(child.indent))
        return items

    def parse_map(self, indent):
        result = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError("unexpected indentation", line.no, line.text)
            if line.body.startswith("- "):
                break
            body = _strip_comment(line.body, line.no)
            key, sep, rest = _partition_key(body)
            if not sep:
                raise YamlError("expected 'key: value'", line.no, line.text)
            key = _key_scalar(key, line.no)
            if key in result:
                raise YamlError(f"duplicate key {key!r}", line.no, line.text)
            self.i += 1
            if rest.strip():
                result[key] = _scalar(rest, line.no)
                continue
            child = self.peek()
            if child is None or child.indent <= indent:
                result[key] = None
                if child is not None and child.indent == indent and child.body.startswith("- "):
                    result[key] = self.parse_seq(indent)
                continue
            result[key] = self.parse_node(child.indent)
        return result


def yaml_loads(text):
    lines = []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.lstrip(" ")
        if not s.strip() or s.startswith("#") or s.strip() == "---":
            lines.append(None)
            continue
        if s.strip() == "...":
            break
        lines.append(_Line(no, raw))
    parser = _YamlParser(lines)
    first = parser.peek()
    if first is None:
        return None
    result = parser.parse_node(first.indent)
    trailing = parser.peek()
    if trailing is not None:
        raise YamlError("trailing content (multiple documents?)", trailing.no, trailing.text)
    return result


