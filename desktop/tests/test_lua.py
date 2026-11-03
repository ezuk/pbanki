import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pbanki_desktop import lua

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real_sidecar"
ASSISTANT_BACKUP = Path.home() / "Documents/GitHub/assistant.koplugin/backups/261025/koreader"

# Prints any Lua data file in one canonical form, using real Lua's own reader
CANONICAL_LUA = r"""
local function esc(s)
    return (s:gsub('[%c"\\]', function(c) return string.format("\\%d;", c:byte()) end))
end
local function canon(v)
    local t = type(v)
    if t == "table" then
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        local function text(k) return type(k) .. ":" .. tostring(k) end
        table.sort(keys, function(a, b) return text(a) < text(b) end)
        local out = {}
        for _, k in ipairs(keys) do out[#out + 1] = "[" .. canon(k) .. "]=" .. canon(v[k]) end
        return "{" .. table.concat(out, ",") .. "}"
    elseif t == "string" then return '"' .. esc(v) .. '"'
    elseif t == "number" then return string.format("%.14g", v)
    else return tostring(v) end
end
io.write(canon(dofile(arg[1])))
"""


def canon(value):
    """Same canonical form as CANONICAL_LUA, from what lua.py parsed."""
    if isinstance(value, list):
        value = {i: v for i, v in enumerate(value, 1)}
    if isinstance(value, dict):
        def key_text(k):
            kind = "boolean" if isinstance(k, bool) else "number" if isinstance(k, (int, float)) else "string"
            return f"{kind}:{lua.tostring(k)}".encode("utf-8")
        parts = [f"[{canon(k)}]={canon(value[k])}" for k in sorted(value, key=key_text)]
        return "{" + ",".join(parts) + "}"
    if isinstance(value, str):
        out = []
        for byte in value.encode("utf-8"):
            if byte < 32 or byte == 127 or byte in (ord('"'), ord("\\")):
                out.append(f"\\{byte};")
            else:
                out.append(chr(byte))
        return '"' + "".join(out) + '"'
    return lua.tostring(value)


class ParsingTests(unittest.TestCase):
    def test_a_koreader_file_shape(self):
        value = lua.loads('-- ./history.lua\nreturn {\n    [1] = {\n        ["time"] = 1793721034,\n'
                          '        ["file"] = "/mnt/ext1/English/book.epub",\n    },\n}\n')
        self.assertEqual(value, [{"time": 1793721034, "file": "/mnt/ext1/English/book.epub"}])

    def test_numbers(self):
        value = lua.loads("{ -5, 0.25, -0.125, 1e3, 1E-2, 0x10, .5, 7., 1e+15, - 2 }")
        self.assertEqual(value, [-5, 0.25, -0.125, 1000.0, 0.01, 16, 0.5, 7.0, 1e15, -2])
        self.assertIs(type(value[0]), int)
        self.assertIs(type(value[3]), float)

    def test_what_dump_writes_for_infinity(self):
        value = lua.loads("{ a = inf, b = -inf, c = nan }")
        self.assertEqual(value["a"], float("inf"))
        self.assertEqual(value["b"], float("-inf"))
        self.assertNotEqual(value["c"], value["c"])

    def test_booleans_and_nil(self):
        self.assertEqual(lua.loads('{ ["a"] = true, ["b"] = false, ["c"] = nil }'), {"a": True, "b": False})
        self.assertIsNone(lua.loads("return nil"))

    def test_sequential_keys_become_a_list_in_any_order(self):
        self.assertEqual(lua.loads('{ [2] = "b", [1] = "a", [3] = "c" }'), ["a", "b", "c"])

    def test_keys_with_holes_stay_a_dict(self):
        self.assertEqual(lua.loads('{ [1] = "a", [3] = "c" }'), {1: "a", 3: "c"})
        self.assertEqual(lua.loads('{ [2] = "b" }'), {2: "b"})
        self.assertEqual(lua.loads('{ [0] = "z", [1] = "a" }'), {0: "z", 1: "a"})

    def test_a_nil_value_makes_a_hole(self):
        self.assertEqual(lua.loads('{ "a", nil, "c" }'), {1: "a", 3: "c"})

    def test_empty_table_is_an_empty_dict(self):
        self.assertEqual(lua.loads("{}"), {})

    def test_mixed_and_bare_keys(self):
        self.assertEqual(lua.loads('{ "a", name = "x", [true] = 1, [1.5] = 2; "b" }'),
                         {1: "a", 2: "b", "name": "x", True: 1, 1.5: 2})

    def test_a_float_key_equal_to_an_integer_is_that_integer(self):
        self.assertEqual(lua.loads('{ [1.0] = "a", [2] = "b" }'), ["a", "b"])

    def test_the_last_of_a_repeated_key_wins(self):
        self.assertEqual(lua.loads('{ a = 1, a = 2 }'), {"a": 2})

    def test_nested_tables(self):
        value = lua.loads('{ ["a"] = { ["b"] = { [1] = { ["c"] = "d" } } } }')
        self.assertEqual(value, {"a": {"b": [{"c": "d"}]}})

    def test_escapes(self):
        value = lua.loads(r'''"tab\t nl\n cr\r bs\\ q\" a\' bell\a back\b ff\f vt\v"''')
        self.assertEqual(value, "tab\t nl\n cr\r bs\\ q\" a' bell\a back\b ff\f vt\v")

    def test_a_backslash_newline_is_a_newline(self):
        self.assertEqual(lua.loads('"one\\\ntwo"'), "one\ntwo")
        self.assertEqual(lua.loads('"one\\\r\ntwo"'), "one\ntwo")

    def test_decimal_escapes_make_bytes_of_one_character(self):
        self.assertEqual(lua.loads(r'"\226\128\156hi\226\128\157 \0 \0651"'), "“hi” \x00 A1")

    def test_later_lua_escapes(self):
        self.assertEqual(lua.loads(r'"\x41\u{201C}a\z   b"'), "A“ab")

    def test_raw_non_ascii_as_text_and_as_bytes(self):
        text = '"“Don’t” Čeština שלום"'
        self.assertEqual(lua.loads(text), "“Don’t” Čeština שלום")
        self.assertEqual(lua.loads(text.encode("utf-8")), "“Don’t” Čeština שלום")

    def test_invalid_utf8_is_replaced(self):
        self.assertEqual(lua.loads(b'"ab\xff"'), "ab�")

    def test_long_strings(self):
        self.assertEqual(lua.loads('[[one\ntwo]]'), "one\ntwo")
        self.assertEqual(lua.loads('[[\nskips the first newline]]'), "skips the first newline")
        self.assertEqual(lua.loads('[==[has ]] and ]=] inside]==]'), "has ]] and ]=] inside")
        self.assertEqual(lua.loads(r'[[no \n escapes]]'), r"no \n escapes")
        self.assertEqual(lua.loads('{ [ [[a key]] ] = 1 }'), {"a key": 1})

    def test_comments(self):
        value = lua.loads(
            "-- first\n--[[ block\ncomment ]] return --[==[ ]] still ]==] { -- side\n  1, --[[ inline ]] 2,\n} -- end\n"
        )
        self.assertEqual(value, [1, 2])

    def test_the_loop_marker_dump_writes(self):
        self.assertEqual(lua.loads('{ a = nil --[[ LOOP:\n    ^------- ]], b = 1 }'), {"b": 1})

    def test_without_return_and_with_a_semicolon(self):
        self.assertEqual(lua.loads("{ 1 }"), [1])
        self.assertEqual(lua.loads("return { 1 };\n"), [1])

    def test_a_byte_order_mark_is_ignored(self):
        self.assertEqual(lua.loads(b"\xef\xbb\xbfreturn 1"), 1)

    def test_bad_input_only_ever_raises_luaerror(self):
        for text in (
            "", "   ", "-- only a comment", "return", "return {", "{ 1, 2", '"open', '"a\nb"', "{ [1 = 2 }",
            "{ [nil] = 1 }", "{ 1 2 }", "print('hi')", "os.execute('x')", "{ a = function() end }",
            "return { } extra", '"bad \\q"', '"bad \\999"', '"bad \\xZZ"', '"bad \\u{110000000}"', "[[open",
            "--[[open", "- true", '- "x"', "{ a == 1 }", "function: builtin#21", "@", "\x00",
        ):
            with self.assertRaises(lua.LuaError, msg=text):
                lua.loads(text)

    def test_deep_nesting_is_refused_not_a_crash(self):
        with self.assertRaises(lua.LuaError):
            lua.loads("{" * 100000)

    def test_error_names_the_line(self):
        with self.assertRaisesRegex(lua.LuaError, "line 3"):
            lua.loads("return {\n 1,\n @\n}")

    def test_random_garbage_only_ever_raises_luaerror(self):
        rng = random.Random(7)
        alphabet = '{}[]=,;"\'\\-.0123456789eExreturnil \n\t\xc4\x8d'
        for _ in range(3000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            try:
                lua.loads(text)
            except lua.LuaError:
                pass

    def test_cut_short_real_file_only_ever_raises_luaerror(self):
        raw = (FIXTURES / "metadata.epub.lua").read_bytes()
        for end in range(0, len(raw) - 2, 97):
            with self.assertRaises(lua.LuaError, msg=end):
                lua.loads(raw[:end])


class HelperTests(unittest.TestCase):
    def test_sequence_walks_like_ipairs(self):
        self.assertEqual(lua.sequence(["a", "b"]), ["a", "b"])
        self.assertEqual(lua.sequence({1: "a", 2: "b", 4: "d"}), ["a", "b"])
        self.assertEqual(lua.sequence({2: "b"}), [])
        self.assertEqual(lua.sequence({}), [])
        self.assertEqual(lua.sequence(None), [])
        self.assertEqual(lua.sequence("text"), [])

    @unittest.skipUnless(shutil.which("luajit"), "luajit not installed")
    def test_tostring_matches_luajit(self):
        values = [12, 12.0, 0, -3, 1793721034, 0.1, 1 / 3, 1e15, 2.5e-7, 123456789012345, 1e100]
        program = "for _, v in ipairs({%s}) do print(tostring(v)) end" % ",".join(repr(v) for v in values)
        out = subprocess.run(["luajit", "-e", program], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual([lua.tostring(v) for v in values], out)

    def test_tostring_of_others(self):
        self.assertEqual(lua.tostring(None), "nil")
        self.assertEqual(lua.tostring(True), "true")
        self.assertEqual(lua.tostring(False), "false")
        self.assertEqual(lua.tostring("a"), "a")


class RealFileTests(unittest.TestCase):
    def test_the_real_sidecar(self):
        data = lua.load_file(FIXTURES / "metadata.epub.lua")
        self.assertEqual(len(data["annotations"]), 8)
        self.assertEqual(data["doc_pages"], 675)
        self.assertEqual(data["doc_props"]["title"], "Pride and Prejudice")
        self.assertEqual(data["copt_h_page_margins"], [10, 10])
        self.assertEqual(data["font_family_fonts"], {})
        self.assertFalse(data["hyphenation"] is False)
        self.assertIn("\n", data["annotations"][3]["text"])
        self.assertTrue(data["annotations"][3]["text"].startswith("Mrs. Bennet deigned"))
        self.assertIn("“Don’t keep coughing", data["annotations"][3]["text"])
        self.assertEqual(data["annotations"][6]["pos0"], "/body/DocFragment[4]/body/p[43]/text().0")

    def assert_same_as_lua(self, path):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "canon.lua"
            program.write_text(CANONICAL_LUA, encoding="utf-8")
            real = subprocess.run(["lua", str(program), str(path)], capture_output=True, check=True)
        self.assertEqual(canon(lua.load_file(path)), real.stdout.decode("latin-1"))

    @unittest.skipUnless(shutil.which("lua"), "lua not installed")
    def test_parsing_equals_real_lua_on_the_fixture(self):
        self.assert_same_as_lua(FIXTURES / "metadata.epub.lua")
        self.assert_same_as_lua(FIXTURES / "history.lua")

    @unittest.skipUnless(shutil.which("lua") and ASSISTANT_BACKUP.is_dir(), "lua or the assistant backup missing")
    def test_parsing_equals_real_lua_on_real_backups(self):
        self.assert_same_as_lua(ASSISTANT_BACKUP / "history.lua")
        self.assert_same_as_lua(ASSISTANT_BACKUP / "settings.reader.lua")

    @unittest.skipUnless(ASSISTANT_BACKUP.is_dir(), "the assistant backup is missing")
    def test_real_history_and_settings(self):
        history = lua.load_file(ASSISTANT_BACKUP / "history.lua")
        self.assertTrue(history[0]["file"].startswith("/mnt/ext1/"))
        self.assertIsInstance(history[0]["time"], int)
        settings = lua.load_file(ASSISTANT_BACKUP / "settings.reader.lua")
        self.assertEqual(settings["document_metadata_folder"], "doc")


class JsonSafetyTests(unittest.TestCase):
    def test_a_parsed_sidecar_with_string_keys_is_json_safe(self):
        data = lua.load_file(FIXTURES / "metadata.epub.lua")
        json.dumps(data, ensure_ascii=False)


class MalformedShapeTests(unittest.TestCase):
    def test_a_table_as_a_key_is_a_lua_error(self):
        for text in ("return {[{}]=1}", "return {[{1}]=2,}", 'return {[{a=1}]="x"}'):
            with self.subTest(text=text):
                with self.assertRaises(lua.LuaError):
                    lua.loads(text)

    def test_half_an_emoji_in_given_text_doesnt_raise(self):
        self.assertEqual(lua.loads('return "a\ud83db"'), "a\ufffd\ufffd\ufffdb")


if __name__ == "__main__":
    unittest.main()
