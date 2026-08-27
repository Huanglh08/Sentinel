"""批量语法校验：用 ast.parse 逐个文件检查，路径作为 argv。"""
import ast
import sys


def main() -> int:
    failed = 0
    for p in sys.argv[1:]:
        try:
            ast.parse(open(p, encoding="utf-8").read(), filename=p)
            print(f"[ok]    {p}")
        except SyntaxError as e:
            failed += 1
            print(f"[FAIL]  {p}  -> {e}")
    if failed:
        print(f"\n{failed} file(s) failed")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
