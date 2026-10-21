from conftest import make_repo
from enguru_lib import blast
from enguru_lib.gitinfo import Hunk


def hunk(path, *lines):
    return Hunk("h1", path, 1, 1, list(lines))


def syms(path, *lines):
    return blast.changed_symbols([hunk(path, *lines)])


def test_python_symbols():
    assert syms("a.py", "+def alpha(x):", "-    async def beta():",
                "+class Gamma:", " def untouched():", "+    x = 1") \
        == {"alpha", "beta", "Gamma"}


def test_js_symbols():
    assert syms("a.ts", "+function foo(a) {",
                "+const bar = async (x) => x",
                "-let baz = y => y",
                "+class Qux {",
                "+  handleClick(e) {",
                "+  if (x) {") == {"foo", "bar", "baz", "Qux", "handleClick"}


def test_go_symbols():
    assert syms("a.go", "+func Run(ctx int) {",
                "+func (s *Server) Handle(w int) {") == {"Run", "Handle"}


def test_java_symbols():
    assert syms("A.java", "+    public static void doWork(int x) {",
                "-    private List<String> names() {") == {"doWork", "names"}


def test_ruby_symbols():
    assert syms("a.rb", "+  def self.build", "+  def helper(x)",
                "+class Widget") == {"build", "helper", "Widget"}


def test_rust_symbols():
    assert syms("a.rs", "+pub fn compute(x: i32) -> i32 {",
                "+struct Point {", "-enum Color {", "+trait Shape {") \
        == {"compute", "Point", "Color", "Shape"}


def test_drop_short_and_generic_names():
    assert syms("a.py", "+def main():", "+def ab():", "+def init():",
                "+def test():", "+def setUp(self):", "+def render():",
                "+def keeper():") == {"keeper"}


def test_fallback_identifiers_for_unknown_ext():
    s = syms("a.txt", "+return config_value from helperThing",
             "-the helperThing is null None true", "+ab xyz self")
    assert s == {"config_value", "helperThing"}


def test_fallback_keeps_ten_most_frequent():
    lines = ["+" + " ".join("name%02d" % i for i in range(12)),
             "+name00 name01"]
    s = syms("a.txt", *lines)
    assert len(s) == 10
    assert {"name00", "name01"} <= s


def test_blast_radius_finds_caller_and_test_excludes_changed(tmp_path):
    r = make_repo(tmp_path, [{
        "lib.py": "def compute():\n    pass\n",
        "app.py": "from lib import compute\nx = compute()\n",
        "tests/test_lib.py": "def test_it():\n    compute()\n",
        ".env": "compute=1\n",
    }], ["init"])
    res = blast.blast_radius(r, {"compute"}, {"lib.py"})
    got = {(d["path"], d["line"], d["kind"]) for d in res}
    assert got == {("app.py", 1, "caller"), ("app.py", 2, "caller"),
                   ("tests/test_lib.py", 2, "test")}
    assert all(d["symbol"] == "compute" and isinstance(d["line"], int)
               for d in res)


def test_blast_radius_no_match_returns_empty(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "x = 1\n"}], ["init"])
    assert blast.blast_radius(r, {"nothing_here"}, set()) == []


def test_cap_respected(tmp_path):
    r = make_repo(tmp_path, [{"a.py": "\n".join(["compute()"] * 10) + "\n"}],
                  ["init"])
    res = blast.blast_radius(r, {"compute"}, set(), cap=3)
    assert len(res) == 3
    assert [d["line"] for d in res] == [1, 2, 3]
