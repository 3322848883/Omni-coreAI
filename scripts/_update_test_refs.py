"""批量更新 skillkit 测试文件引用到夹具根。"""
from pathlib import Path

FILES = [
    "test_skillkit_angles.py",
    "test_skillkit_round3.py",
    "test_skillkit_round4.py",
    "test_skillkit_round5.py",
    "test_skillkit_round6.py",
    "test_skillkit_round7.py",
]

NEW_SKILLS = (
    "from tests._skill_fixtures import fixture_root, fixture_skills_dir  # noqa: E402\n"
    "\n"
    "SKILLS = fixture_skills_dir()"
)

for fn in FILES:
    p = Path("tests") / fn
    t = p.read_text(encoding="utf-8")
    orig = t
    t = t.replace('SKILLS = ROOT / "skills"', NEW_SKILLS)
    t = t.replace("bot_root=str(ROOT)", "bot_root=str(fixture_root())")
    t = t.replace("root=ROOT", "root=fixture_root()")
    if t != orig:
        p.write_text(t, encoding="utf-8")
        print("updated", fn)
    else:
        print("no change", fn)
