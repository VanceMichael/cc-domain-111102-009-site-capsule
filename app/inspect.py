"""只读链接检查入口：打印单个 HTML 的引用结构，不发起网络请求。"""

from pathlib import Path

from app.references import inspect_links


def main() -> None:
    print(
        inspect_links(
            Path("fixtures/index.html").read_text(encoding="utf-8"),
            "https://exhibition.invalid/show/index.html",
        )
    )


if __name__ == "__main__":
    main()
