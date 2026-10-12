"""Minimal S-expression reader/writer for KiCad files (py3.9, no deps)."""
import re

_tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse(text):
    stack = [[]]
    for m in _tok.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(Str(t[1:-1].replace('\\"', '"').replace("\\\\", "\\")))
        else:
            stack[-1].append(t)
    return stack[0]


class Str(str):
    """A quoted string, so the writer re-quotes it."""


def dump(node, indent=0):
    if isinstance(node, list):
        if not node:
            return "()"
        simple = all(not isinstance(x, list) for x in node)
        if simple and len(node) < 12:
            return "(" + " ".join(dump(x) for x in node) + ")"
        pad = "\t" * (indent + 1)
        head = [dump(x) for x in node if not isinstance(x, list)]
        out = "(" + " ".join(head)
        for x in node:
            if isinstance(x, list):
                out += "\n" + pad + dump(x, indent + 1)
        return out + "\n" + "\t" * indent + ")"
    if isinstance(node, Str):
        return '"' + node.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(node, float):
        s = ("%.4f" % node).rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(node)


def find(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def first(node, key):
    r = find(node, key)
    return r[0] if r else None
