"""Safe parser for KOReader's Lua data files: `-- path` comment, then `return { ... }`.

Reads data only. Nothing is evaluated and no Lua program runs.

How Lua values come back:
  nil -> None, true/false -> bool, numbers -> int or float, strings -> str.
  A table whose keys are exactly 1..n -> list. Any other table -> dict, with int, float,
  bool or str keys as written, so a table with holes such as {[1]=a, [3]=b} stays a dict.
  An empty table -> {}, which can be iterated and asked .get() alike.
  A key set to nil is left out, as in Lua.

Strings are decoded from UTF-8 after escapes are applied, because \\ddd stands for one byte
of a possibly longer character. Bytes that aren't valid UTF-8 become U+FFFD.
"""

import re

MAX_DEPTH = 200

_SPACE = re.compile(rb"[ \t\r\n\f\v]+")
_LONG_OPEN = re.compile(rb"\[(=*)\[")
_NAME = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(rb"0[xX][0-9a-fA-F]+|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PLAIN = re.compile(rb"[^\"'\\\r\n]+")
_ESCAPES = {
    ord("a"): b"\a",
    ord("b"): b"\b",
    ord("f"): b"\f",
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("v"): b"\v",
    ord("\\"): b"\\",
    ord('"'): b'"',
    ord("'"): b"'",
    ord("\n"): b"\n",
}
_WORDS = {
    b"nil": None,
    b"true": True,
    b"false": False,
    # What KOReader's dump writes for numbers Lua can't spell
    b"inf": float("inf"),
    b"nan": float("nan"),
}
_ABSENT = object()


class LuaError(ValueError):
    """The text isn't a Lua data file this parser understands."""


class _Parser:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def fail(self, message):
        line = self.data.count(b"\n", 0, min(self.pos, len(self.data))) + 1
        raise LuaError(f"line {line}: {message}")

    def skip(self):
        data = self.data
        while True:
            match = _SPACE.match(data, self.pos)
            if match:
                self.pos = match.end()
            if not data.startswith(b"--", self.pos):
                return
            self.pos += 2
            long_open = _LONG_OPEN.match(data, self.pos)
            if long_open:
                self.long_string(long_open)
            else:
                end = data.find(b"\n", self.pos)
                self.pos = len(data) if end < 0 else end + 1

    def long_string(self, opening):
        close = b"]" + opening.group(1) + b"]"
        start = opening.end()
        end = self.data.find(close, start)
        if end < 0:
            self.fail("long string or comment is not closed")
        self.pos = end + len(close)
        # A newline straight after the opening is not part of the string
        if self.data.startswith(b"\r\n", start) or self.data.startswith(b"\n\r", start):
            start += 2
        elif self.data[start:start + 1] in (b"\n", b"\r"):
            start += 1
        return self.data[start:end]

    def quoted(self, quote):
        data = self.data
        self.pos += 1
        out = []
        while True:
            match = _PLAIN.match(data, self.pos)
            if match:
                out.append(match.group())
                self.pos = match.end()
            if self.pos >= len(data):
                self.fail("string is not closed")
            char = data[self.pos]
            if char == quote:
                self.pos += 1
                return b"".join(out)
            if char in (0x0a, 0x0d):
                self.fail("string runs over a line")
            if char in (0x22, 0x27):
                out.append(bytes((char,)))
                self.pos += 1
                continue
            out.append(self.escape())

    def escape(self):
        data = self.data
        self.pos += 1
        if self.pos >= len(data):
            self.fail("string is not closed")
        char = data[self.pos]
        if char == 0x0d:
            self.pos += 2 if data[self.pos + 1:self.pos + 2] == b"\n" else 1
            return b"\n"
        if char == 0x0a and data[self.pos + 1:self.pos + 2] == b"\r":
            self.pos += 2
            return b"\n"
        if char in _ESCAPES:
            self.pos += 1
            return _ESCAPES[char]
        if 0x30 <= char <= 0x39:
            end = self.pos + 1
            while end < self.pos + 3 and data[end:end + 1].isdigit():
                end += 1
            value = int(data[self.pos:end])
            if value > 255:
                self.fail("escape is out of range")
            self.pos = end
            return bytes((value,))
        if char == ord("x"):
            digits = data[self.pos + 1:self.pos + 3]
            try:
                value = int(digits, 16) if len(digits) == 2 else -1
            except ValueError:
                value = -1
            if value < 0:
                self.fail("bad \\x escape")
            self.pos += 3
            return bytes((value,))
        if char == ord("z"):
            self.pos += 1
            match = _SPACE.match(data, self.pos)
            if match:
                self.pos = match.end()
            return b""
        if char == ord("u"):
            end = data.find(b"}", self.pos)
            try:
                if data[self.pos + 1:self.pos + 2] != b"{" or end < 0:
                    raise ValueError
                value = int(data[self.pos + 2:end], 16)
                encoded = chr(value).encode("utf-8", "surrogatepass")
            except (ValueError, OverflowError):
                self.fail("bad \\u escape")
            self.pos = end + 1
            return encoded
        self.fail("unknown escape in string")

    def string(self):
        char = self.data[self.pos:self.pos + 1]
        if char in (b'"', b"'"):
            raw = self.quoted(char[0])
        else:
            raw = self.long_string(_LONG_OPEN.match(self.data, self.pos))
        return raw.decode("utf-8", "replace")

    def number(self):
        match = _NUMBER.match(self.data, self.pos)
        if not match:
            self.fail("expected a value")
        self.pos = match.end()
        text = match.group().decode("ascii")
        if text[:2] in ("0x", "0X"):
            return int(text, 16)
        if text.isdigit():
            try:
                return int(text)
            except ValueError:
                self.fail("number is too long")
        return float(text)

    def value(self, depth):
        self.skip()
        data = self.data
        char = data[self.pos:self.pos + 1]
        if char == b"":
            self.fail("file ended where a value was expected")
        if char == b"{":
            return self.table(depth + 1)
        if char in (b'"', b"'") or _LONG_OPEN.match(data, self.pos):
            return self.string()
        if char == b"-":
            self.pos += 1
            self.skip()
            value = self.value(depth)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self.fail("minus sign before something that isn't a number")
            return -value
        name = _NAME.match(data, self.pos)
        if name:
            if name.group() not in _WORDS:
                self.fail(f"unexpected word {name.group().decode('ascii')!r}")
            self.pos = name.end()
            return _WORDS[name.group()]
        return self.number()

    def key(self, depth):
        """The key of a table field, or _ABSENT when the field is positional."""
        data = self.data
        char = data[self.pos:self.pos + 1]
        if char == b"[" and not _LONG_OPEN.match(data, self.pos):
            self.pos += 1
            key = self.value(depth)
            self.skip()
            if data[self.pos:self.pos + 1] != b"]":
                self.fail("expected ] after a table key")
            self.pos += 1
            self.skip()
            if data[self.pos:self.pos + 1] != b"=":
                self.fail("expected = after a table key")
            self.pos += 1
            if key is None:
                self.fail("a table key can't be nil")
            return key
        name = _NAME.match(data, self.pos)
        if name and name.group() not in _WORDS:
            after = _SPACE.match(data, name.end())
            end = after.end() if after else name.end()
            if data[end:end + 1] == b"=" and data[end:end + 2] != b"==":
                self.pos = end + 1
                return name.group().decode("ascii")
        return _ABSENT

    def table(self, depth):
        if depth > MAX_DEPTH:
            self.fail("tables are nested too deeply")
        self.pos += 1
        fields = {}
        next_index = 1
        while True:
            self.skip()
            char = self.data[self.pos:self.pos + 1]
            if char == b"}":
                self.pos += 1
                return _shape(fields)
            if char == b"":
                self.fail("table is not closed")
            key = self.key(depth)
            value = self.value(depth)
            if key is _ABSENT:
                key = next_index
                next_index += 1
            if isinstance(key, (dict, list)):
                self.fail("a table can't be a key here")
            # 1.0 and 1 are the same key in Lua
            if isinstance(key, float) and key.is_integer():
                key = int(key)
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
            self.skip()
            char = self.data[self.pos:self.pos + 1]
            if char in (b",", b";"):
                self.pos += 1
            elif char != b"}":
                self.fail("expected , or } in a table")


def _shape(fields):
    size = len(fields)
    if size and all(type(key) is int for key in fields):
        if all(i in fields for i in range(1, size + 1)):
            return [fields[i] for i in range(1, size + 1)]
    return fields


def loads(text):
    """Value of a Lua data file given as str or bytes. Raises LuaError on anything else."""
    data = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else bytes(text)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    parser = _Parser(data)
    try:
        parser.skip()
        name = _NAME.match(data, parser.pos)
        if name and name.group() == b"return":
            parser.pos = name.end()
        value = parser.value(0)
        parser.skip()
        if data[parser.pos:parser.pos + 1] == b";":
            parser.pos += 1
            parser.skip()
    except RecursionError:
        raise LuaError("tables are nested too deeply") from None
    if parser.pos < len(data):
        parser.fail("unexpected text after the value")
    return value


def load_file(path):
    """Value of the Lua data file at `path`. Raises LuaError or OSError."""
    with open(path, "rb") as f:
        return loads(f.read())


def sequence(value):
    """What Lua's ipairs would walk: a list as it is, a table with holes up to its first hole."""
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    out = []
    while len(out) + 1 in value:
        out.append(value[len(out) + 1])
    return out


def tostring(value):
    """Lua's tostring for data values, as LuaJIT prints them. Tables have no stable text."""
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return "nan"
        if number in (float("inf"), float("-inf")):
            return "inf" if number > 0 else "-inf"
        return format(number, ".14g")
    return "table"
