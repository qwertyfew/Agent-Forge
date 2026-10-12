"""
创建测试脚本 tools/verify_edit_guard.py：验证 edit_file 和 write_file 的语法检查及回滚行为。
参考 tools/verify_efficiency.py 的断言风格。
"""

import sys
from pathlib import Path
import tempfile
import json
import os
import shutil

# 为了能够导入 agentforge 里的工具，需要模拟环境或直接从包中导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from agentforge.tools.fs import build_fs_tools, _syntax_check

fails: list[str] = []

def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + f"  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)

# --- 测试环境准备 ---
tmp_dir = Path(tempfile.mkdtemp(prefix="af_guard_test_"))

def setup_file(name, content):
    p = tmp_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    return p

try:
    # 构造一个最小配置以初始化工具
    class MockGuard:
        def resolve(self, path):
            # 将相对路径映射到临时目录，如果已经是绝对路径或包含路径则处理
            p = Path(path)
            if p.is_absolute(): return p
            return tmp_dir / p.name

    class MockCfg:
        max_tool_output_chars = 1000
    
    mock_guard = MockGuard()
    tools = build_fs_tools(mock_guard, MockCfg())
    
    # 工具列表索引：0:read_file, 1:write_file, 2:edit_file, 3:delete_file, 4:list_dir
    write_file_tool = tools[1]
    edit_file_tool = tools[2]

    # --- 测试用例 ---

    # 1. edit_file 把合法 py 改坏 -> 被回滚且返回错误、文件内容不变
    p1 = setup_file("test1.py", "x = 1\nprint(x)")
    original_content_1 = p1.read_text(encoding="utf-8")
    res1 = edit_file_tool.run(path="test1.py", old_text="x = 1", new_text="x = 1\nprint(x")  # 故意漏掉括号
    check("edit_file 把合法 py 改坏 -> 被回滚且返回错误", "[错误]" in res1 and p1.read_text(encoding="utf-8") == original_content_1)

    # 2. edit_file 正确修改 -> 正常应用
    p2 = setup_file("test2.py", "x = 1\nprint(x)")
    res2 = edit_file_tool.run(path="test2.py", old_text="print(x)", new_text="print(x + 1)")
    check("edit_file 正确修改 -> 正常应用", "已修改" in res2 and p2.read_text(encoding="utf-8") == "x = 1\nprint(x + 1)")

    # 3. write_file 覆盖合法 py 为非法内容 -> 回滚
    p3 = setup_file("test3.py", "x = 1\nprint(x)")
    original_content_3 = p3.read_text(encoding="utf-8")
    res3 = write_file_tool.run(path="test3.py", content="x = 1\nprint(x") # 语法错误
    check("write_file 覆盖合法 py 为非法内容 -> 回滚", "[错误]" in res3 and p3.read_text(encoding="utf-8") == original_content_3)

    # 4. write_file 新建 py（哪怕语法错）-> 不拦截
    p4 = tmp_dir / "test4.py"
    # 先确保文件不存在
    if p4.exists(): p4.unlink()
    res4 = write_file_tool.run(path="test4.py", content="x = 1\nprint(x") # 语法错误，但新建不拦截
    check("write_file 新建 py（哪怕语法错）-> 不拦截", "已新建" in res4 and p4.read_text(encoding="utf-8") == "x = 1\nprint(x")

    # 5. json 文件写坏 -> 回滚
    p5 = setup_file("test5.json", '{"key": "value"}')
    original_content_5 = p5.read_text(encoding="utf-8")
    res5 = write_file_tool.run(path="test5.json", content='{"key": "value"') # JSON 语法错误
    check("json 文件写坏 -> 回滚", "[错误]" in res5 and p5.read_text(encoding="utf-8") == original_content_5)

    # 6. 非 py/json 文件 -> 不检查不拦截
    p6 = setup_file("test6.txt", "some content")
    res6 = write_file_tool.run(path="test6.txt", content="bad syntax content") # 虽然是非法内容但不是py/json
    check("非 py/json 文件 -> 不检查不拦截", "已覆盖" in res6 and p6.read_text(encoding="utf-8") == "bad syntax content")

finally:
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)

print()
if fails:
    print(f"{len(fails)} 项失败：{fails}")
    sys.exit(1)
print("ALL CHECKS PASSED")
